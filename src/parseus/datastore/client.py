"""Client for interaction with Elasticsearch and Opensearch."""

from collections import ChainMap
from datetime import datetime, timezone
from time import sleep
from typing import Dict, Iterable, Iterator, List, Tuple, Union

from elasticsearch import Elasticsearch
from elasticsearch.helpers import parallel_bulk as elasticsearch_parallel_bulk
from opensearchpy import OpenSearch
from opensearchpy.helpers import parallel_bulk as opensearch_parallel_bulk

from parseus.datastore import LOGGER
from parseus.util.datetime_utils import datetime_parser


class DatastoreShardFailure(RuntimeError):
    """Raised when a datastore search succeeds at the request level but one or
    more shards failed to return their data.

    Elasticsearch/OpenSearch return **HTTP 200** for a search even when some
    shards errored: the per-shard outcome is reported in ``_shards`` (``failed``
    count and a ``failures`` list), and ``hits.hits`` then contains only the
    results from the shards that *did* succeed. Silently returning that partial
    page makes a caller believe it has the complete result set when it does not
    -- for the alerts watchfloor that means an operator can be shown a short,
    plausible-looking alert list that is missing real detections, with no error
    anywhere. Treating a shard failure as a hard error (rather than returning a
    truncated page) is the only way a trust-critical consumer can tell "there
    are no more alerts" apart from "some shards could not be read".
    """
def get_datastore(datastore_host_url, datastore_type, datastore_auth=None):
    """
    Gets a datastore host object
    """
    LOGGER.debug("Grabbing data from elastic search located at %s", datastore_host_url)
    if datastore_auth:
        datastore = DataStore(
            datastore_host_url, basic_auth=datastore_auth, base=datastore_type
        )
    else:
        datastore = DataStore(datastore_host_url, base=datastore_type)
    return datastore


def _url_with_credentials(url, username, password):
    """Inject ``username:password`` into ``url`` if both are given."""
    if username and password:
        base, sep, hostname = url.partition("//")
        return f"{base}{sep}{username}:{password}@{hostname}"
    if username and password is None:
        LOGGER.error("Please provide a password if providing a username!")
    elif username is None and password:
        LOGGER.error("Please provide a username if providing a password!")
    return url


def check_datastore(ds_client):
    """
    Verifies that the datastore is available for use.
    If successful returns True, else raises an exception.
    """
    if ds_client is None:
        raise ValueError("Datastore object did not exist.")
    if not ds_client.url:
        raise ValueError("Datastore url was blank")
    if not ds_client.ping():
        raise ConnectionError(f"Datastore host '{ds_client.url}' could not be reached")
    return True


# Need to disable errors for class attributes due to the meta-Inheritance
# pylint: disable=E1101
class DBInheritanceMeta(type):
    """
    Making datastore compatible with either Opensearch or ElasticsearchDatabase
    via inheritance.

    NOTE: Opensearch is a fork of elasticsearch from Version 7.
    """

    def __call__(cls, *args, **kwargs):
        base = kwargs.pop("base", None)
        # If DataStore is instantiated with a base parameter, the instance
        # will subclass from the base class passed in.
        # Expected to be opensearch object but that is on the instantiator to pass correctly
        if base == "opensearch":
            # Dynamically create a new class with the specified base class
            opensearch_cls = type(cls.__name__, (cls, OpenSearch), {})
            return super(DBInheritanceMeta, opensearch_cls).__call__(*args, **kwargs)
        # For backwards compatibility, if no base parameter is passed, defaults to
        # subclassing Elasticsearch as was original intent
        elasticsearch_cls = type(cls.__name__, (cls, Elasticsearch), {})
        return super(DBInheritanceMeta, elasticsearch_cls).__call__(*args, **kwargs)


class DataStore(metaclass=DBInheritanceMeta):
    """
    Elasticsearch / Opensearch client with method overrides to simplify queries and aggregations
    Additional methods make the default behaviour lazier.
    """

    def __init__(
        self,
        url: str,
        ca_certs: Union[str, None] = None,
        client_cert: Union[str, None] = None,
        client_key: Union[str, None] = None,
        username: Union[str, None] = None,
        password: Union[str, None] = None,
        basic_auth=None,
        base: Union[str, None] = None,
    ):
        """
        :param url: The URL endpoint to the {Open,Elastic}search instance (i.e. http://localhost:9200)
        :param ca_certs: Path to CA certificates directory. {Server authentication}
        :param client_cert: Path to client certificate. {Client authentication}
        :param client_key: Path to client private key. {Client authentication}
        :param username: Username for basic authentication (used with ``password``).
        :param password: Password for basic authentication (used with ``username``).
        :param basic_auth: Used for basic authentication. Expecting a tuple of (<user>, <pass>)
        """
        # Get the name of the first parent class
        parent_class_name = self.__class__.__bases__[1].__name__
        if parent_class_name == "OpenSearch":
            self.helper_func = opensearch_parallel_bulk
        else:
            self.helper_func = elasticsearch_parallel_bulk
        url = _url_with_credentials(url, username, password)

        ssl = url.startswith("https")
        if ssl and ca_certs is None:
            LOGGER.warning(
                "Provide CA certificate to properly use HTTPS. Continuing without verifying."
            )
            ssl = False

        super().__init__(
            url,
            verify_certs=ssl,
            ca_certs=ca_certs,
            client_cert=client_cert,
            client_key=client_key,
            request_timeout=90,
            http_auth=basic_auth,
            ssl_show_warn=False,
        )

        self.url = url
        self.log = LOGGER

    def index_exists(self, index: str) -> bool:
        """Checks whether an index exists."""
        return bool(self.indices.exists(index=index, allow_no_indices=False))

    def create_index(self, index: str, mappings: Dict = None) -> bool:
        """Creates an index in the datastore.

        :param index: The index to create.
        :return: The status of the create opeartion as a bool.
        """
        if not index.islower():
            self.log.error(
                "Unable to create index: %s. Index names must be lowercase!", index
            )

            return False
        try:
            self.log.info("Created index %s", index)
            body = {}
            if mappings:
                body["mappings"] = mappings
            return bool(
                self.indices.create(index=index, body=body).get("acknowledged", False)
            )
        except Exception as err:
            self.log.error("Unable to create index %s: %s", index, err)
            return False

    def ensure_index_template(
        self, name: str, index_patterns: List[str], mappings: Dict
    ) -> bool:
        """Idempotently install a legacy index template pinning field mappings.

        Daily indices (e.g. ``alerts-<name>-YYYY.MM.DD``) are otherwise created
        with purely *dynamic* mappings, so a field's type can differ between
        days depending on the first document written to each index -- a
        ``keyword`` subfield may or may not exist, ``handled`` may be unmapped on
        a day whose first alert omitted it, etc. That drift makes the alerts
        queries (exact ``term`` on ``.keyword`` fields, boolean ``handled``
        filter, script sort) behave inconsistently or fail a shard on some
        indices. A template applied to the index pattern pins those fields so
        every index created from now on has a stable, correct mapping.

        Uses the legacy ``_template`` API, which both Elasticsearch 7/8 and
        OpenSearch support, for maximum compatibility. Best-effort and
        idempotent: a failure is logged and returns ``False`` rather than
        raising, so an inability to install the template never breaks a write or
        a query (the fields simply fall back to dynamic mapping as before).

        :param name: The template name.
        :param index_patterns: Index patterns the template applies to.
        :param mappings: The ``mappings`` block to pin.
        :return: ``True`` if the template is present/installed, else ``False``.
        """
        try:
            body = {"index_patterns": index_patterns, "mappings": mappings}
            return bool(
                self.indices.put_template(name=name, body=body).get(
                    "acknowledged", False
                )
            )
        except Exception as err:
            self.log.error("Unable to install index template %s: %s", name, err)
            return False

    def delete_index(self, index: str) -> bool:
        """Removes an index from Elasticsearch and all its data.

        :param index: The index to delete.
        :return: The status of the delete operation as a bool.
        """
        if not self.index_exists(index):
            self.log.error("Unable to delete index %s. Does not exist!", index)
            return False
        try:
            self.log.info("Successfully deleted index %s", index)
            return bool(self.indices.delete(index=index)["acknowledged"])
        except Exception as err:
            self.log.error("Unable to delete index %s: %s", index, err)

            return False

    def clear_index(self, index: str) -> bool:
        """Removes all documents from an Elasticsearch index.

        :param index: The index to clear.
        :return: The status of the clear operation as a bool.
        """
        if self.index_exists(index):
            return self.delete_by_query(index)

        self.log.error("Unable to clear index: %s. Does not exist!", index)

        return False

    def reindex(self, src: str, dst: str) -> bool:
        """Copies an Elasticsearch index from 'src' to 'dst'.

        :param src: The source index to copy from.
        :param dst: The destination index to copy to.
        :return: The status of the copy (reindex) as a bool.
        """
        if not self.index_exists(src):
            self.log.error("%s index does not exist!", src)
            return False
        body = {"source": {"index": src}, "dest": {"index": dst}}
        super().reindex(body=body, refresh=True)
        return True

    def delete_by_query(
        self, index: str, query: Union[Dict, List, None] = None
    ) -> bool:
        """Deletes all documents from an Elasticsearch index matching a query.

        :param index: The index to query.
        :param query: The query to run.
        :raises ValueError: If multiple queries are provided.
        :return: The status of the delete_by_query operation as a bool.
        """
        if query is None:
            query = {"match_all": {}}
        if not self.index_exists(index):
            self.log.warning(f"Unable to delete from {index}. Does not exist!")
            return False

        query = query if isinstance(query, Dict) else {"bool": {"must": query}}

        return bool(
            super()
            .delete_by_query(index=index, body={"query": query}, refresh=True)
            .get("deleted", False)
        )

    def get_document_count(self, index: str, query: Union[Dict, None] = None) -> int:
        """Gets the number of documents in an index.

        :param index: The index to query.
        :return: The size of the index as an int.
        """
        return (
            self.count(index=index, body={"query": {"bool": {"must": query}}}).get(
                "count", 0
            )
            if self.index_exists(index)
            else 0
        )

    def get_min_timestamp(self, index: str) -> datetime:
        """Returns the smallest timestamp in an index, or beginning of the epoch (1/1/1970).

        :param index: The index to query.
        :return: The smallest timestamp in the index, or :code:`datetime(1970, 1, 1, tzinfo=timezone.utc)`.
        """
        if not self.index_exists(index) or not self.get_document_count(index):
            timestamp = datetime.fromtimestamp(0, tz=timezone.utc)
        else:
            body = {
                "query": {"match_all": {}},
                "sort": [{"@timestamp": {"order": "asc"}}],
                "size": 1,
            }

            # Execute the search
            results = self.search(index=index, body=body)
            # Process the results
            try:
                for hit in results["hits"]["hits"]:
                    timestamp = datetime_parser(hit["_source"]["@timestamp"])
            except KeyError:
                timestamp = datetime.fromtimestamp(0, tz=timezone.utc)
        return timestamp

    def get_max_timestamp(self, index: str) -> datetime:
        """Returns the largest timestamp in an index, or the current time.

        :param index: The index to query.
        :return: The largest timestamp in the index, or :code:`datetime.now(timezone.utc)`.
        """
        if not self.index_exists(index) or not self.get_document_count(index):
            timestamp = datetime.now(tz=timezone.utc)
        else:
            body = {
                "query": {"match_all": {}},
                "sort": [{"@timestamp": {"order": "desc"}}],
                "size": 1,
            }
            # Execute the search
            results = self.search(index=index, body=body)
            # Process the results
            try:
                for hit in results["hits"]["hits"]:
                    timestamp = datetime_parser(hit["_source"]["@timestamp"])
            except KeyError:
                timestamp = datetime.now(tz=timezone.utc)
        return timestamp

    def bulk_push_docs(self, index: str, docs: Iterable[Dict]) -> bool:
        """Pushes documents from an iterable of dictionaries into an {Open,Elastic}search index.

        :param index: The index to push documents to.
        :param docs: Iterable of documents (dictionaries).
        :return: Status of the push as a bool.
        """
        if not self.index_exists(index):
            self.create_index(index)
        # Convert docs into a generator if necessary, do push
        response = self.helper_func(
            client=self,
            index=index,
            actions=iter(docs) if not isinstance(docs, Iterator) else docs,
        )

        # Iterate through results
        # - Necessary to make docs available.. weird API behavior
        for success, info in response:
            if not success:
                self.log.error(info)
                return False

        # Refresh index to make new docs available for search
        self.indices.refresh(index=index)

        return True

    def wait_for_documents(
        self,
        index: str,
        max_seconds: int = None,
        min_documents: int = 1,
        poll_interval: float = 0.1,
    ) -> None:
        """Polls an index and waits for documents to appear in an index before continuing.

        Useful for unit tests.

        :param index: The index to poll
        :param max_seconds: The maximum number of seconds to wait.
        :param min_documents: The minimum number of documents that must be in
        the index. None/0 to wait forever.
        :param poll_interval: The interval, in seconds, to poll.
        :raises TimeoutError: If `max_seconds` reached and there are still not enough documents
        """
        if not self.index_exists(index):
            self.log.error(
                f"Failed to wait for documents in index {index}. Does not exist!"
            )
            raise ValueError

        # Start waiting
        start_time = datetime.now(timezone.utc)
        while (
            not max_seconds
            or (datetime.now(timezone.utc) - start_time).total_seconds() < max_seconds
        ):
            if self.get_document_count(index) >= min_documents:
                return None

            sleep(poll_interval)

        raise TimeoutError

    def _raise_on_shard_failures(self, result: Dict, *, context: str) -> None:
        """Raise :class:`DatastoreShardFailure` if a search result has failed shards.

        A search/scroll response reports partial-shard failures in ``_shards``
        (``_shards.failed`` > 0 with details in ``_shards.failures``) while still
        returning HTTP 200 and whatever hits the healthy shards produced. This
        inspects that block and refuses to pass a silently-truncated page back to
        the caller.

        :param result: The raw search/scroll response body.
        :param context: Short description of the operation, for the error/log.
        :raises DatastoreShardFailure: When ``_shards.failed`` is non-zero.
        """
        shards = result.get("_shards") or {}
        failed = shards.get("failed") or 0
        if not failed:
            return
        failures = shards.get("failures") or []
        detail = "; ".join(str(failure.get("reason", failure)) for failure in failures[:5])
        total = shards.get("total")
        message = (
            f"{context}: {failed} of {total} shard(s) failed; "
            f"results would be incomplete. {detail}"
        )
        self.log.error(message)
        raise DatastoreShardFailure(message)

    def start_scroll(
        self,
        index: str,
        timeout: str = "5m",
        size: int = 10000,
        query: Dict = None,
        sort: Union[Dict, List, None] = None,
        return_fields: Union[List, None] = None,
    ) -> Tuple[Union[str, None], List]:
        """Starts a scroll context on an {Open,Elastics}earch index.

        wrap_query provides the query if none is passed in.

        :param index: The index to query.
        :param timeout: The period of time to maintain the scroll context.
        :param size: The number of documents in each scroll (max 10000).
        :return: A tuple (_scroll_id, hits), where scroll_id will be used
        when calling continue_scroll() to resume scrolling.
        :raises DatastoreShardFailure: When the search reports failed shards, so
            a partial (silently-truncated) page is never returned as if complete.
        """
        body = {}
        if query:
            body["query"] = query
        if size:
            body["size"] = size
        if sort:
            body["sort"] = sort
        if return_fields:
            body["_source"] = return_fields

        result = self.search(
            index=index,
            body=body,
            scroll=timeout,
        )

        if "error" in result:
            self.log.error(f"Error running query: {result}")
            return None, []

        self._raise_on_shard_failures(result, context=f"scroll search on '{index}'")

        # Collect scroll ID and any results
        scroll_id = result.get("_scroll_id", None)
        results = (
            [hit["_source"] for hit in result["hits"]["hits"]]
            if result["hits"]["total"]["value"] > 0
            else []
        )

        return (scroll_id, results)

    def continue_scroll(
        self, scroll_id: Union[str, None], timeout: str = "5m"
    ) -> Tuple[Union[str, None], List]:
        """Resumes a previously-started scroll context.

        :param scroll_id: The scroll ID, as returned from the last call to
                          start_scroll() or continue_scroll().
        :param timeout: The period of time to maintain the scroll context.
        :return: A tuple (scroll_id, hits), where scroll_id will be used
        when calling continue_scroll() to resume scrolling.
        :raises ValueError: When the scroll is completed or expires.
        :raises DatastoreShardFailure: When the scroll reports failed shards, so
            a partial (silently-truncated) page is never returned as if complete.
        """
        if scroll_id is None:
            return None, []

        result = self.scroll(scroll_id=scroll_id, scroll=timeout)

        # Handle scroll expiration, no more hits
        if "error" in result:
            if result["error"]["type"] == "search_phase_execution_exception":
                return None, []
        elif not result["hits"]["hits"]:
            return None, []

        self._raise_on_shard_failures(result, context="scroll continuation")

        # Collect scroll ID and any results
        scroll_id = result.get("_scroll_id", None)
        results = (
            [hit["_source"] for hit in result["hits"]["hits"]]
            if result["hits"]["total"]["value"] > 0
            else []
        )

        return (scroll_id, results)


    def get_all_entries(
        self,
        index: str,
        query: Dict = None,
        sort: Dict = None,
        timeout: str = "5m",
        size: int = 10000,
    ) -> Union[Iterator[Dict], None]:
        """Queries {Open,Elastic}search using the Scroll API to get all documents in
        the index matching an optional query.

        :param index: The index to use.
        :param size: Number of items to fetch per request to the Scroll API.
        :param timeout: The period of time to maintain the scroll context.
            (renewed after fetching every <size> items)
        :param query: The query to execute, for example:
            :code:`{ "match" : { "func" : "READ_COILS" } }`

        :return: A sequence of entries (list of dictionaries).
        """
        if query is None:
            query = {"match_all": {}}
        # Check if index exists
        if not self.index_exists(index):
            return
        # Start the scroll, get first results
        scroll_id, results = self.start_scroll(
            index, query=query, sort=sort, timeout=timeout, size=size
        )
        # Yield any results from the intitial query, or return
        if results:
            yield from results
        else:
            if scroll_id is not None:
                self.clear_scroll(scroll_id=scroll_id)
                scroll_id = None
        # Continue scroll until context ends or no more results are found
        while scroll_id is not None:
            scroll_id, results = self.continue_scroll(scroll_id, timeout=timeout)
            if results:
                yield from results
            else:
                if scroll_id is not None:
                    self.clear_scroll(scroll_id=scroll_id)


    def search_top(
        self,
        index: str,
        query: Union[Dict, None] = None,
        sort: Union[Dict, List, None] = None,
        size: int = 10000,
        return_fields: Union[List, None] = None,
    ) -> Tuple[List[Dict], int]:
        """Return at most ``size`` matching documents plus the total match count.

        A single bounded search (no scroll context). This is the right tool for a
        UI that only needs the newest N results with a sort applied -- unlike the
        scrolling :meth:`get_all_entries`/``DSData`` path, it never materialises an
        unbounded result set and never opens a server-side scroll context, so a
        large or growing index cannot turn one query into an ever-larger fetch.

        A missing index (or a pattern that matches nothing) is answered by the
        search itself -- ``ignore_unavailable``/``allow_no_indices`` make the
        datastore return an empty result instead of an error -- rather than by a
        separate ``index_exists`` round trip first. That halves the requests for a
        frequently polled query and removes the check-then-search race in which
        the index appears or disappears between the two calls.

        :param index: The index (pattern) to query.
        :param query: The query body; defaults to ``match_all``.
        :param sort: Optional sort specification.
        :param size: Maximum number of documents to return.
        :param return_fields: Optional ``_source`` field restriction.
        :return: ``(documents, total_matches)`` where ``documents`` is at most
            ``size`` items and ``total_matches`` is the full count of documents
            the query matched (so the UI can show "showing N of M").
        :raises DatastoreShardFailure: When the search reports failed shards, so a
            silently-truncated page is never returned as if it were complete.
        """
        body: Dict = {"size": size, "track_total_hits": True}
        body["query"] = query if query else {"match_all": {}}
        if sort:
            body["sort"] = sort
        if return_fields:
            body["_source"] = return_fields

        result = self.search(
            index=index,
            body=body,
            ignore_unavailable=True,
            allow_no_indices=True,
        )

        if "error" in result:
            self.log.error("Error running bounded search on '%s': %s", index, result)
            raise DatastoreShardFailure(
                f"bounded search on '{index}' failed: {result.get('error')}"
            )

        self._raise_on_shard_failures(result, context=f"bounded search on '{index}'")

        hits = result.get("hits", {})
        documents = [hit["_source"] for hit in hits.get("hits", [])]
        total = hits.get("total", {})
        total_matches = total.get("value", len(documents)) if isinstance(total, dict) else total
        return documents, total_matches


    def get_time_range_entries(
        self,
        index: str,
        start: Union[str, None] = None,
        end: Union[str, None] = None,
        query: Union[Dict, List, None] = None,
    ):  # -> Generator[Dict, None]:
        """Get all documents within a specified time range.

        :param index: The index to query.
        :param start: The timestamp from which to start pulling from (inclusive).
        :param end: The timestamp from which to stop pulling (inclusive).
        :param query: Query to run.

        :return: generator that generates one or more dictionaries of results.
        """

        time_range = {}
        if start is not None:
            time_range.update({"gte": start})
        if end is not None:
            time_range.update({"lt": end})

        if time_range:
            range_query = {"bool": {"filter": {"range": {"@timestamp": time_range}}}}
            if not query or "match_all" in query:
                query = range_query
            else:
                query.update(range_query)

        yield from self.get_all_entries(index, query=query)

    def get_aggs(
        self, index: str, aggs: Union[dict, list], query: dict = None
    ) -> Union[dict, None]:
        """Add aggs to query and only return agg results.

        :param index: The index to run an aggregation on.
        :param aggs: The aggregation as a dictionary, excluding the "aggs" outer key.
        :param query: The query to apply.
        return: The aggregation results as a dictionary.
        """
        if not self.index_exists(index):
            return None

        if not query:
            query = {"match_all": {}}

        # Merge the list of aggregations into a single agg
        if isinstance(aggs, List):
            aggs = dict(ChainMap(*aggs))

        body = {"query": query, "aggs": aggs, "size": 0}

        # Get aggregation
        result = self.search(index=index, body=body)

        # Need to check classes here... this is straighforward
        return result
