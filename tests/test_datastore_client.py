"""
Tests for the Datastore Class helper functions and methods implemented in this project.
Not attempting to test functions and methods implemented by the libraries the class inherits from.
"""

from datetime import datetime

from parseus.datastore import client

from .globals import DATASTORE, DATASTORE_HOST_URL, DATASTORE_TYPE

TEST_INDEX = "test-index"
TEST_MAPPING = {
    "date_detection": True,
    "properties": {"@timestamp": {"type": "date"}, "value": {"type": "short"}},
}


def test_check_datastore():
    "verify 'check_datastore' raises no exceptions when using test's global datastore"
    client.check_datastore(ds_client=DATASTORE)


def test_get_datastore():
    "verify 'check_datastore' raises no exceptions when using results of 'get_datastore'"
    datastore = client.get_datastore(DATASTORE_HOST_URL, DATASTORE_TYPE)
    client.check_datastore(ds_client=datastore)


def test_index_create_exists_delete():
    """
    Verify that the datastore is able to create and delete an index.
    Checks the existence of the index between each step.
    """
    datastore = client.get_datastore(DATASTORE_HOST_URL, DATASTORE_TYPE)
    exists = datastore.index_exists(index=TEST_INDEX)
    assert not exists
    success = datastore.create_index(index=TEST_INDEX, mappings=TEST_MAPPING)
    assert success
    exists = datastore.index_exists(index=TEST_INDEX)
    assert exists
    success = datastore.delete_index(index=TEST_INDEX)
    assert success
    exists = datastore.index_exists(index=TEST_INDEX)
    assert not exists


def test_push_clear_get_docs():
    datastore = client.get_datastore(DATASTORE_HOST_URL, DATASTORE_TYPE)
    # Add docs and verify that the right number got pushed.
    docs = []
    for i in range(42):
        doc = {}
        doc["date"] = datetime.now()
        doc["value"] = i
        docs.append(doc)
    datastore.bulk_push_docs(index=TEST_INDEX, docs=docs)
    exists = datastore.index_exists(index=TEST_INDEX)
    assert exists
    doc_count = datastore.get_document_count(index=TEST_INDEX)
    assert doc_count == 42
    # Scroll to get docs one at a time
    retrieved_docs = datastore.get_all_entries(index=TEST_INDEX, size=1)
    assert len(list(retrieved_docs)) == 42
    # Verify that clearing the index reduces the count to 0
    datastore.clear_index(index=TEST_INDEX)
    doc_count = datastore.get_document_count(index=TEST_INDEX)
    assert doc_count == 0
    # index cleanup
    success = datastore.delete_index(index=TEST_INDEX)
    assert success
    exists = datastore.index_exists(index=TEST_INDEX)
    assert not exists
