import base64
import json
import os

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils

from mercari_alert import client as client_mod
from mercari_alert import main as main_mod
from mercari_alert.analyzer import compute_market, filter_items, find_deals
from mercari_alert.client import Item, MercariClient, MercariError, generate_dpop
from mercari_alert.state import SeenStore


def b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def api_item(id, name, price, status="ITEM_STATUS_ON_SALE", cond=3, **kw):
    d = {"id": id, "name": name, "price": str(price), "status": status,
         "thumbnails": [f"https://example.invalid/{id}.jpg"], "itemConditionId": str(cond),
         "created": "1700000000", "itemType": "ITEM_TYPE_MERCARI"}
    d.update(kw)
    return d


# ---------------------------------------------------------------- DPoP

def test_dpop_signature_is_valid():
    tok = generate_dpop("https://api.mercari.jp/v2/entities:search", "post")
    h, p, sig = tok.split(".")
    header, payload = json.loads(b64d(h)), json.loads(b64d(p))
    assert header["typ"] == "dpop+jwt" and header["alg"] == "ES256"
    assert payload["htm"] == "POST" and payload["htu"].endswith("entities:search")
    raw = b64d(sig)
    assert len(raw) == 64
    pub = ec.EllipticCurvePublicNumbers(
        int.from_bytes(b64d(header["jwk"]["x"]), "big"),
        int.from_bytes(b64d(header["jwk"]["y"]), "big"),
        ec.SECP256R1(),
    ).public_key()
    der = utils.encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    pub.verify(der, f"{h}.{p}".encode(), ec.ECDSA(hashes.SHA256()))  # 例外なら失敗


# ---------------------------------------------------------------- Item

def test_item_parsing_and_urls():
    it = Item.from_api(api_item("m12345", "時計", 1000))
    assert it.price == 1000 and it.url == "https://jp.mercari.com/item/m12345"
    assert not it.is_shop and it.condition_id == 3
    shop = Item.from_api(api_item("2abcDEF", "時計", 1000, itemType="ITEM_TYPE_BEYOND"))
    assert shop.is_shop and shop.url == "https://jp.mercari.com/shops/product/2abcDEF"
    assert Item.from_api({"id": "m1"}) is None  # price 欠落はスキップ


# ---------------------------------------------------------------- 判定

def items(*rows):
    return [Item.from_api(api_item(*r)) for r in rows]


def test_filter_excludes_accessories_and_range():
    xs = items(("m1", "ロレックス 箱のみ", 50000), ("m2", "ロレックス 本体 外箱あり", 900000),
               ("m3", "ロレックス 本体", 5000), ("m4", "ロレックス 本体", 900000, "ITEM_STATUS_ON_SALE", 6))
    out = filter_items(xs, ["箱のみ"], price_min=100000, condition_ids=[1, 2, 3, 4])
    assert [i.id for i in out] == ["m2"]


def test_market_and_deals():
    sold = items(*[(f"m{i}", "x", p) for i, p in enumerate([100, 110, 90, 105, 95])])
    on_sale = items(("m10", "a", 70), ("m11", "b", 80), ("m12", "c", 20), ("m13", "d", 99))
    m = compute_market(sold, on_sale, reference_price=None, min_sold_samples=5)
    assert m.price == 100 and m.source == "sold_median"
    deals = find_deals(on_sale, m, discount_threshold=0.25, suspicious_ratio=0.35)
    assert [d.item.id for d in deals] == ["m12", "m10"]  # 80 は 20% 引きなので対象外
    assert deals[0].suspicious and not deals[1].suspicious


def test_market_reference_and_insufficient():
    sold = items(("m1", "x", 100))
    assert compute_market(sold, [], None, 5).price is None
    m = compute_market(sold, [], 200, 5)
    assert m.price == 200 and m.source == "reference"


# ---------------------------------------------------------------- 状態

def test_seen_store_price_drop(tmp_path):
    p = tmp_path / "seen.json"
    st = SeenStore(str(p))
    assert st.should_alert("m1", 100) == (True, None)
    st.mark("m1", 100, "w")
    st.save()
    st2 = SeenStore(str(p))
    assert st2.should_alert("m1", 100) == (False, None)
    assert st2.should_alert("m1", 90) == (True, 100)


# ---------------------------------------------------------------- HTTP を模擬した全体テスト

class FakeResp:
    def __init__(self, code, body):
        self.status_code, self._body = code, body
        self.text = json.dumps(body, ensure_ascii=False)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def post(self, url, headers, data, timeout):
        body = json.loads(data.decode("utf-8"))
        self.calls.append((url, headers, body))
        return self.handler(body)


def test_400_falls_back_to_no_server_filters(monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)

    def handler(body):
        if "priceMin" in body["searchCondition"]:
            return FakeResp(400, {"code": 3})
        return FakeResp(200, {"items": [api_item("m1", "a", 10)], "meta": {}})

    sess = FakeSession(handler)
    c = MercariClient(interval_sec=(0, 0), session=sess)
    out = c.search("時計", price_min=5)
    assert [i.id for i in out] == ["m1"] and len(sess.calls) == 2
    assert sess.calls[0][1]["X-Platform"] == "web" and sess.calls[0][1]["DPoP"]


def test_403_raises_without_retry(monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)
    sess = FakeSession(lambda b: FakeResp(403, {"error": "forbidden"}))
    with pytest.raises(MercariError) as e:
        MercariClient(interval_sec=(0, 0), session=sess).search("x")
    assert e.value.status_code == 403 and len(sess.calls) == 1


def test_end_to_end(monkeypatch, tmp_path):
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)

    def handler(body):
        if body["searchCondition"]["status"] == ["STATUS_SOLD_OUT"]:
            rows = [api_item(f"m9{i}", "サブマリーナ", p, "ITEM_STATUS_SOLD_OUT")
                    for i, p in enumerate([1_000_000, 1_100_000, 950_000, 1_050_000, 980_000, 20_000])]
            rows.append(api_item("m99", "サブマリーナ 箱のみ", 30_000, "ITEM_STATUS_SOLD_OUT"))
        else:
            rows = [api_item("m1", "サブマリーナ 美品", 700_000),
                    api_item("m2", "サブマリーナ", 990_000),
                    api_item("m3", "サブマリーナ 箱のみ", 20_000)]
        return FakeResp(200, {"items": rows, "meta": {"nextPageToken": ""}})

    sess = FakeSession(handler)
    real_init = MercariClient.__init__

    def init(self, *a, **kw):
        kw["session"] = sess
        kw["interval_sec"] = (0, 0)
        real_init(self, *a, **kw)

    monkeypatch.setattr(MercariClient, "__init__", init)

    sent = []
    monkeypatch.setattr(main_mod.DiscordNotifier, "_send", lambda self, p: sent.append(p))
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.invalid/x")

    cfg = tmp_path / "config.yaml"
    cfg.write_text("settings: {min_sold_samples: 5}\nwatches:\n"
                   "  - {name: サブ, keyword: サブマリーナ, price_min: 100000}\n", encoding="utf-8")
    state = tmp_path / "seen.json"

    assert main_mod.main(["--config", str(cfg), "--state", str(state)]) == 0
    deal_msgs = [p for p in sent if p.get("embeds")]
    assert len(deal_msgs) == 1
    embeds = deal_msgs[0]["embeds"]
    assert [e["url"] for e in embeds] == ["https://jp.mercari.com/item/m1"]
    assert "30% 安い" in embeds[0]["description"]
    assert "m1" in json.loads(state.read_text(encoding="utf-8"))

    # 2 回目は同じ商品を通知しない
    sent.clear()
    assert main_mod.main(["--config", str(cfg), "--state", str(state)]) == 0
    assert not [p for p in sent if p.get("embeds")]
    assert any("チェック完了" in p.get("content", "") for p in sent)


def test_default_config_is_valid():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = main_mod.load_config(os.path.join(base, "config.yaml"))
    assert all(w.get("keyword") for w in cfg["watches"])
