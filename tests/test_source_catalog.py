from monitoring import api, native_schema


def test_catalog_exposes_registered_source_capabilities_without_querying_metrics():
    value=api.source_catalog()
    assert {s['id'] for s in value['sources']}==set(native_schema.NODES)
    for source in value['sources']:
        assert source['nodes']==native_schema.NODES[source['id']]
        assert source['semantics'] and source['method'] and source['revision']
        assert 'backend_url' not in source  # Source discovery is not endpoint identity evidence.
