"""メルカリ検索クライアント（非公式API）

メルカリWeb版（jp.mercari.com）が内部で呼び出している検索API
`https://api.mercari.jp/v2/entities:search` を利用する。
公開APIではないため、仕様は予告なく変わる可能性がある。

認証には DPoP ヘッダ（ES256 署名の JWT）が必要。ログインは不要で、
リクエスト毎に使い捨ての鍵を生成して署名する。
"""

from __future__ import annotations

import base64
import json
import logging
import random
import re
import time
import uuid
from dataclasses import dataclass, field

import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils

log = logging.getLogger(__name__)

SEARCH_URL = "https://api.mercari.jp/v2/entities:search"
ITEM_URL = "https://jp.mercari.com/item/{id}"
SHOPS_ITEM_URL = "https://jp.mercari.com/shops/product/{id}"

STATUS_ON_SALE = "STATUS_ON_SALE"
STATUS_SOLD_OUT = "STATUS_SOLD_OUT"


class MercariError(Exception):
    """API 呼び出しの失敗"""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------- DPoP


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _int_bytes(n: int, length: int = 32) -> bytes:
    # P-256 の座標・署名値は 32 バイト固定長で表現する
    return n.to_bytes(length, byteorder="big")


def generate_dpop(url: str, method: str = "POST") -> str:
    """DPoP トークンを生成する（リクエスト毎に新しい鍵を使う）"""
    key = ec.generate_private_key(ec.SECP256R1())
    pub = key.public_key().public_numbers()
    header = {
        "typ": "dpop+jwt",
        "alg": "ES256",
        "jwk": {
            "crv": "P-256",
            "kty": "EC",
            "x": _b64url(_int_bytes(pub.x)),
            "y": _b64url(_int_bytes(pub.y)),
        },
    }
    payload = {
        "iat": int(time.time()),
        "jti": str(uuid.uuid4()),
        "htu": url,
        "htm": method.upper(),
    }
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    )
    der = key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(der)
    return signing_input + "." + _b64url(_int_bytes(r) + _int_bytes(s))


# ---------------------------------------------------------------- データ型


@dataclass
class Item:
    id: str
    name: str
    price: int
    status: str
    url: str
    thumbnail: str = ""
    condition_id: int | None = None
    created: int | None = None
    is_shop: bool = False
    is_auction: bool = False
    raw: dict = field(default_factory=dict, repr=False)

    @staticmethod
    def from_api(d: dict) -> "Item | None":
        try:
            item_id = str(d["id"])
            price = int(float(d["price"]))
        except (KeyError, TypeError, ValueError):
            log.warning("解析できない商品データをスキップ: %s", str(d)[:200])
            return None
        item_type = str(d.get("itemType", ""))
        # 通常出品のIDは「m + 数字」、メルカリShopsはそれ以外の英数字
        is_shop = "BEYOND" in item_type or not re.fullmatch(r"m\d+", item_id)
        url = (SHOPS_ITEM_URL if is_shop else ITEM_URL).format(id=item_id)
        thumbs = d.get("thumbnails") or []
        cond = d.get("itemConditionId")
        created = d.get("created")
        return Item(
            id=item_id,
            name=str(d.get("name", "")),
            price=price,
            status=str(d.get("status", "")),
            url=url,
            thumbnail=thumbs[0] if thumbs else "",
            condition_id=int(cond) if str(cond).isdigit() else None,
            created=int(created) if str(created).isdigit() else None,
            is_shop=is_shop,
            is_auction=bool(d.get("auction")),
            raw=d,
        )


# ---------------------------------------------------------------- クライアント


class MercariClient:
    def __init__(
        self,
        interval_sec: tuple[float, float] = (4.0, 8.0),
        timeout: float = 20.0,
        max_retries: int = 3,
        user_agent: str = "python-mercari",
        session: requests.Session | None = None,
    ):
        self.interval_sec = interval_sec
        self.timeout = timeout
        self.max_retries = max_retries
        self.user_agent = user_agent
        self.session = session or requests.Session()
        self._last_request = 0.0

    def _wait(self) -> None:
        """前回リクエストから一定時間（ランダム）空ける"""
        wait = random.uniform(*self.interval_sec)
        elapsed = time.monotonic() - self._last_request
        if self._last_request and elapsed < wait:
            time.sleep(wait - elapsed)

    def _post(self, body: dict) -> dict:
        last_err: MercariError | None = None
        for attempt in range(1, self.max_retries + 1):
            self._wait()
            headers = {
                "DPoP": generate_dpop(SEARCH_URL, "POST"),
                "X-Platform": "web",
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": self.user_agent,
            }
            try:
                resp = self.session.post(
                    SEARCH_URL,
                    headers=headers,
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    timeout=self.timeout,
                )
            except requests.RequestException as e:
                last_err = MercariError(f"通信エラー: {e}")
            else:
                self._last_request = time.monotonic()
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError:
                        last_err = MercariError("応答がJSONではありません", 200)
                elif resp.status_code in (429, 500, 502, 503, 504):
                    last_err = MercariError(
                        f"HTTP {resp.status_code}: {resp.text[:200]}", resp.status_code
                    )
                else:
                    # 400/401/403 などは再試行しても変わらないので即座に返す
                    raise MercariError(
                        f"HTTP {resp.status_code}: {resp.text[:300]}", resp.status_code
                    )
            self._last_request = time.monotonic()
            backoff = 15 * attempt
            log.warning("検索失敗 (%d/%d回目): %s → %d秒後に再試行",
                        attempt, self.max_retries, last_err, backoff)
            time.sleep(backoff)
        assert last_err is not None
        raise last_err

    def search(
        self,
        keyword: str,
        status: str = STATUS_ON_SALE,
        exclude_keyword: str = "",
        price_min: int | None = None,
        price_max: int | None = None,
        condition_ids: list[int] | None = None,
        page_size: int = 120,
        server_side_filters: bool = True,
    ) -> list[Item]:
        """新着順で 1 ページ分（最大 page_size 件）を取得する"""
        cond: dict = {
            "keyword": keyword,
            "excludeKeyword": exclude_keyword,
            "sort": "SORT_CREATED_TIME",
            "order": "ORDER_DESC",
            "status": [status],
        }
        filters: dict = {}
        if price_min is not None:
            filters["priceMin"] = int(price_min)
        if price_max is not None:
            filters["priceMax"] = int(price_max)
        if condition_ids:
            filters["itemConditionId"] = [int(c) for c in condition_ids]

        body = {
            "userId": "",
            "pageSize": page_size,
            "pageToken": "v1:0",
            "searchSessionId": uuid.uuid4().hex,
            "indexRouting": "INDEX_ROUTING_UNSPECIFIED",
            "searchCondition": {**cond, **(filters if server_side_filters else {})},
            "defaultDatasets": ["DATASET_TYPE_MERCARI", "DATASET_TYPE_BEYOND"],
            "withAuction": True,
        }
        try:
            data = self._post(body)
        except MercariError as e:
            # 絞り込み条件のフィールド名が変わった場合などに備え、
            # 400 なら条件なしで再検索し、絞り込みはローカル側に任せる
            if e.status_code == 400 and filters and server_side_filters:
                log.warning("絞り込み条件付き検索が400のため、条件なしで再検索します")
                body["searchCondition"] = cond
                data = self._post(body)
            else:
                raise
        items = [Item.from_api(d) for d in data.get("items") or []]
        return [i for i in items if i is not None]
