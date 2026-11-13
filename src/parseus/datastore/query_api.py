"""
Functions to create and use datastore queries.
"""

from copy import deepcopy

from parseus.datastore import LOGGER
from parseus.util.datetime_utils import datetime_parser, make_zulu_time

from .data import DSData
from .peat import PeatDocumentData


def push(ds_client, index, document_list, overwrite_old=True, grouping=None):
    """Pushes documents to an index.

    document_list: The list of documents (or document iterable) to push
    overwrite_old: If True, any document with @timestamp >= the smallest
                   @timestamp in document_list will be deleted.
                   If False, no document will be deleted and the provided
                   document list will be added to the index.
    """
    # first, delete documents after the first result timestamp
    if overwrite_old and document_list:
        query = {"bool": {}}
        query["bool"].setdefault("must", [])
        if grouping:
            grouped_values = set(doc[grouping] for doc in document_list)
            for value in grouped_values:
                grouped_min_timestamp = min(
                    item["@timestamp"]
                    for item in document_list
                    if item[grouping] == value
                )
                query["bool"]["must"].append({"match_phrase": {grouping: value}})
                query["bool"]["must"].append(
                    {"range": {"@timestamp": {"gte": grouped_min_timestamp}}}
                )
                ds_client.delete_by_query(index, query)
                query["bool"]["must"].pop()
                query["bool"]["must"].pop()
        else:
            min_timestamp = min(item["@timestamp"] for item in document_list)
            query["bool"]["must"].append(
                {"range": {"@timestamp": {"gte": min_timestamp}}}
            )
            ds_client.delete_by_query(index, query)

    ds_client.bulk_push_docs(index, document_list)


def pull(
    ds_client, index, query_config, start=None, end=None, block_size=None, sort=None
):
    """
    Pulls data from a range of DateTimes, returns a corresponding DocumentData object.
    """
    query = query_config.get("query", {})
    query = deepcopy(query)
    if query and ("bool" not in query):
        query = {"bool": {"must": [query]}}
    if start is not None:
        query = add_timerange(query, start, end)
    return DSData(
        ds_client,
        index,
        query=query,
        aggs=query_config.get("aggregations", None),
        composite_agg=query_config.get("composite_aggregations", None),
        es_return_fields=query_config.get("return_fields", None),
        block_size=block_size,
        sort=sort if sort else {"@timestamp": {"order": "asc"}},
    )


def get_modbus_registers(
    ds_client, index, start=None, end=None, ip_filter="", logger=LOGGER
):
    query = get_modbus_registers_query()
    return_fields = get_modbus_return_fields()

    return get_datastore_data(
        ds_client,
        index,
        start,
        end,
        ip_filter,
        logger=logger,
        query=query,
        return_fields=return_fields,
    )


def get_dnp3_points(ds_client, index, start=None, end=None, ip_filter="", logger=LOGGER):
    """Return the DNP3 point-value documents written by the Zeek ingestor."""
    return get_datastore_data(
        ds_client,
        index,
        start,
        end,
        ip_filter,
        logger=logger,
        query=get_dnp3_points_query(),
        return_fields=get_dnp3_return_fields(),
    )


def get_modbus_function_codes(
    ds_client,
    index,
    start=None,
    end=None,
    ip_filter="",
    logger=LOGGER,
    calendar_interval="1d",
):
    fc_filter = get_modbus_function_code_queries()
    fc_aggs = get_modbus_fc_aggs(calendar_interval)

    return get_datastore_data(
        ds_client=ds_client,
        index=index,
        start=start,
        end=end,
        ip_filter=ip_filter,
        logger=logger,
        query=fc_filter,
        composite_aggs=fc_aggs,
    )


def get_peat_data(path):
    """Return PEAT filesystem data for a run or devices directory."""
    return PeatDocumentData(directory_path=path)


def get_datastore_data(
    ds_client,
    index,
    start=None,
    end=None,
    ip_filter="",
    logger=LOGGER,
    query=None,
    aggs=None,
    composite_aggs=None,
    return_fields=None,
    sort=None,
):
    """
    Queries datastore client for its index, start and end times, and a list of other
    filter parameters.

    :param ds_client: database client to get data from
    :param index: datastore index
    :param start: earliest time to get documents from
    :param end: latest time to get documents from
    :param logger: logger object
    :param query: filter list to add to query (reduces bandwidth of response)
    :param aggs: Optional argument incase we are looking for aggregations from the datastore
    :param return_fields: List of fields datastore should return;
                          saves bandwidth and memory over returning all fields for every document
    :return: ESData object that will lazily return documents
    """
    start = datetime_parser(start)
    end = datetime_parser(end)
    if query:
        logger.info("Queries used on index are %s", query)
        logger.info("Returning fields: %s", return_fields)
    query = add_filter(query, build_ip_filter(ip_filter))
    query_config = {
        "aggregations": aggs,
        "composite_aggregations": composite_aggs,
        "query": query,
        "return_fields": return_fields,
    }
    data = pull(
        ds_client=ds_client,
        index=index,
        query_config=query_config,
        start=start,
        end=end,
        sort=sort,
    )
    return data


def get_modbus_registers_query():
    protocol_query = {
        "bool": {
            "must": [{"term": {"event.dataset": "modbus_detailed"}}],
            "should": [
                {"exists": {"field": "zeek.modbus_detailed.request_values"}},
                {"exists": {"field": "zeek.modbus_detailed.response_values"}},
            ],
            "minimum_should_match": 1,
        }
    }
    return protocol_query


def get_modbus_return_fields():
    return [
        "source.ip",
        "destination.ip",
        "zeek.modbus_detailed.response_values",
        "zeek.modbus_detailed.request_values",
        "zeek.modbus_detailed.address",
        "zeek.modbus.func",
        "@timestamp",
    ]


def get_dnp3_points_query():
    return {
        "bool": {
            "must": [
                {"term": {"event.dataset": "dnp3_points"}},
                {"exists": {"field": "zeek.dnp3_points.value"}},
            ]
        }
    }


def get_dnp3_return_fields():
    return [
        "source.ip",
        "destination.ip",
        "zeek.dnp3_points",
        "@timestamp",
    ]


def get_modbus_function_code_queries():
    query = {"bool": {"filter": {"term": {"event.dataset": "modbus"}}, "must": []}}
    return query


def get_modbus_fc_aggs(calendar_interval="1d"):
    aggs = {
        "aggs": {
            "composite": {
                "composite": {
                    "size": 10000,
                    "sources": [
                        {
                            "date": {
                                "date_histogram": {
                                    "field": "@timestamp",
                                    "calendar_interval": calendar_interval,
                                }
                            }
                        },
                        {"source_ips": {"terms": {"field": "source.ip"}}},
                        {"destination_ips": {"terms": {"field": "destination.ip"}}},
                    ],
                },
                "aggs": {
                    "func_codes": {
                        "terms": {"field": "zeek.modbus.func", "size": 10000}
                    }
                },
            }
        }
    }
    return aggs


def build_ip_filter(ips_to_filter):
    """
    Takes a list of IPs and creates a 'filter' that can be added to an
    datastore style query
    """
    if ips_to_filter:
        return {
            "bool": {
                "minimum_should_match": 1,
                "should": [
                    {"match_phrase": {"source.ip": ips_to_filter}},
                    {"match_phrase": {"destination.ip": ips_to_filter}},
                ],
            }
        }
    return None


def add_timerange(query, start, end):
    "Takes a elasticsearch query, returns a copy with date range added"
    time_range = {"gte": make_zulu_time(start)}
    if end is not None:
        time_range["lt"] = make_zulu_time(end)
    range_query = {"range": {"@timestamp": time_range}}
    return add_must_sub_query(query, range_query)


def add_filter(query, filters):
    """
    Adds filter field to query. Robust to a variety of query
    and filter formats.

    Returns a new query object and does not mutate the caller's query.
    """
    query = deepcopy(query)
    filters = deepcopy(filters)
    if not filters:
        return query
    if query is None:
        query = {"bool": {}}
    elif "bool" not in query:
        query = {"bool": {"must": [query]}}
    if "filter" not in query["bool"]:
        query["bool"]["filter"] = [{"match_all": {}}]
    else:
        existing_filter = query["bool"]["filter"]
        if isinstance(existing_filter, dict):
            query["bool"]["filter"] = [existing_filter]
    if isinstance(filters, list):
        query["bool"]["filter"].extend(filters)
    elif isinstance(filters, dict):
        query["bool"]["filter"].append(filters)
    return query


def add_must_sub_query(query, sub_query):
    """
    Adds a sub-query to the 'must' list of a boolean query
    in an {Open,Elastic}search query dictionary.

    Parameters:
    query (dict): Original query dictionary.
    sub_query (dict): Sub-query to be added.

    Returns:
    dict: The updated query dictionary with the new sub-query added.
    """
    if not isinstance(query, dict):
        raise ValueError(
            "add_must_sub_query(query, sub_query): query must be a dictionary."
        )
    if not isinstance(sub_query, dict):
        raise ValueError(
            "add_must_sub_query(query, sub_query): sub_query must be a dictionary."
        )
    query = deepcopy(query)
    if "bool" not in query:
        query = {"bool": {}}
    if "must" not in query["bool"]:
        query["bool"]["must"] = []
    if not isinstance(query["bool"]["must"], list):
        raise ValueError(
            "add_must_sub_query(query, sub_query): query's 'must' has to be a list."
        )
    query["bool"]["must"].append(sub_query)
    return query
