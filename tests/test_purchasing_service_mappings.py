from types import SimpleNamespace

import partpilot.web.features.purchasing.service as service


def _stub_clients(monkeypatch):
    monkeypatch.setattr(service.DigiKeyCredentials, "from_env",
                        classmethod(lambda cls, sandbox=False: SimpleNamespace(locale_currency="SEK")))
    monkeypatch.setattr(service.MouserCredentials, "from_env", classmethod(lambda cls: None))
    import partpilot.digikey.client as dk
    monkeypatch.setattr(dk, "DigiKeyClient", lambda creds: object())


def test_resolve_bom_file_uses_only_given_mappings(tmp_path, monkeypatch):
    _stub_clients(monkeypatch)
    captured = {}

    def fake_resolve_bom(lines, client, settings, build_qty, lookup, stock, mouser, reel):
        captured["lines"] = lines
        return []
    monkeypatch.setattr(service, "resolve_bom", fake_resolve_bom)

    bom = tmp_path / "b.csv"
    # "Value" would normally be detected as value; the user mapped only Wert -> value and
    # deliberately ignored "Value" and "Designator".
    bom.write_text("Designator,Wert,Value,Menge\nR1,10k,IGNORED,2\n")
    service.resolve_bom_file(bom, build_qty=1, check_stock=False,
                             mappings={"value": ["Wert"], "qty": ["Menge"]})
    [line] = captured["lines"]
    assert line.value == "10k" and line.qty == 2
    assert line.refdes == [] and line.device is None


def test_resolve_bom_file_defaults_to_yaml_mappings(tmp_path, monkeypatch):
    _stub_clients(monkeypatch)
    captured = {}

    def fake_resolve_bom(lines, *a):
        captured["lines"] = lines
        return []
    monkeypatch.setattr(service, "resolve_bom", fake_resolve_bom)
    bom = tmp_path / "b.csv"
    bom.write_text("Designator,Value,Qty\nR1,10k,1\n")
    service.resolve_bom_file(bom, build_qty=1, check_stock=False)
    assert captured["lines"][0].refdes == ["R1"]


def test_duplicate_headers_map_to_the_chosen_column(tmp_path, monkeypatch):
    """Two 'Description' columns: the deduped name lets the user pick the second one."""
    _stub_clients(monkeypatch)
    captured = {}

    def fake_resolve_bom(lines, *a):
        captured["lines"] = lines
        return []
    monkeypatch.setattr(service, "resolve_bom", fake_resolve_bom)
    bom = tmp_path / "b.csv"
    bom.write_text("Designator,Value,Description,Description\nR1,10k,first,second\n")
    service.resolve_bom_file(bom, build_qty=1, check_stock=False,
                             mappings={"value": ["Value"], "description": ["Description (2)"]})
    assert captured["lines"][0].description == "second"
