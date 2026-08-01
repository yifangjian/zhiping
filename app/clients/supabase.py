"""Supabase(PostgREST)非同步用戶端。

為什麼不用官方的 supabase-py:官方套件是同步的,在 FastAPI 的事件迴圈裡呼叫會
卡住整個 worker。本專案對延遲敏感(見 docs/DESIGN.md 5.2 的時間預算),所以直接
用 httpx 打 PostgREST,保持全非同步、每次呼叫都有明確 timeout。

只實作用得到的動作,不做成通用 ORM。
"""

import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# PostgREST 把 Postgres 的 unique violation 以 23505 回報
UNIQUE_VIOLATION = "23505"


class SupabaseError(RuntimeError):
    """Supabase 呼叫失敗。"""


class DuplicateRecordError(SupabaseError):
    """違反 unique 限制。用於 webhook 事件去重(見 docs/DESIGN.md 5.4)。"""


class SupabaseClient:
    def __init__(self, url: str, service_role_key: str, timeout: float = 3.0) -> None:
        self._client = httpx.AsyncClient(
            base_url="{}/rest/v1".format(url.rstrip("/")),
            headers={
                "apikey": service_role_key,
                "Authorization": "Bearer {}".format(service_role_key),
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def insert(
        self, table: str, row: Dict[str, Any], returning: bool = True
    ) -> Optional[Dict[str, Any]]:
        """新增一筆資料。違反 unique 限制時丟 DuplicateRecordError。"""
        headers = {
            "Prefer": "return=representation" if returning else "return=minimal"
        }
        rows = await self._request(
            "POST", "/{}".format(table), json=[row], headers=headers
        )
        return rows[0] if rows else None

    async def upsert(
        self, table: str, row: Dict[str, Any], on_conflict: str
    ) -> Optional[Dict[str, Any]]:
        """有就更新、沒有就新增。

        PostgREST 的作法是 POST 加上 `Prefer: resolution=merge-duplicates`,
        搭配 on_conflict 指定要看哪個欄位判斷重複。省掉「先查再決定」的往返。
        """
        rows = await self._request(
            "POST",
            "/{}".format(table),
            params={"on_conflict": on_conflict},
            json=[row],
            headers={
                "Prefer": "resolution=merge-duplicates,return=representation"
            },
        )
        return rows[0] if rows else None

    async def select(
        self, table: str, params: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """查詢資料。params 直接是 PostgREST 的 query string 語法。"""
        return await self._request("GET", "/{}".format(table), params=params)

    async def update(
        self, table: str, params: Dict[str, Any], values: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """更新符合條件的資料,回傳更新後的內容。"""
        return await self._request(
            "PATCH",
            "/{}".format(table),
            params=params,
            json=values,
            headers={"Prefer": "return=representation"},
        )

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json: Any = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> List[Dict[str, Any]]:
        try:
            response = await self._client.request(
                method, path, params=params, json=json, headers=headers
            )
        except httpx.HTTPError as exc:
            raise SupabaseError("Supabase 連線失敗:{}".format(exc)) from exc

        if response.status_code == httpx.codes.CONFLICT:
            raise DuplicateRecordError(_error_detail(response))

        if not response.is_success:
            raise SupabaseError(
                "Supabase {} {} 回傳 {}:{}".format(
                    method, path, response.status_code, _error_detail(response)
                )
            )

        if not response.content:
            return []

        payload = response.json()
        # PostgREST 單筆查詢也可能回 dict,統一成 list 讓呼叫端只處理一種形狀
        if isinstance(payload, dict):
            return [payload]
        return payload


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(body, dict):
        code = body.get("code")
        message = body.get("message") or body.get("hint") or ""
        if code == UNIQUE_VIOLATION:
            return "unique violation: {}".format(message)
        return "{} {}".format(code or "", message).strip()
    return str(body)[:500]
