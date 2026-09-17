"""Small, consent-based public collector feed.

There are no synthetic posts, private collection details, direct messages, or
ranking claims.  A post becomes visible only while its author has deliberately
enabled their public Cardr profile.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from accounts import initialize_accounts, require_user
from database import get_connection


router = APIRouter()
MAX_POST_CHARS = 600


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_post(value: Any) -> str:
    text = str(value or "").replace("\x00", "").strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        raise ValueError("Write a short collector update before posting.")
    if len(text) > MAX_POST_CHARS:
        raise ValueError("Collector updates must be {} characters or fewer.".format(MAX_POST_CHARS))
    return text


@router.get("/api/community/feed")
async def community_feed(limit: int = 24) -> Dict[str, Any]:
    try:
        limit = max(1, min(int(limit), 60))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Feed limit is invalid.")
    initialize_accounts()
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT community_posts.id, community_posts.body, community_posts.created_at,
                   users.display_name, users.profile_handle
            FROM community_posts
            JOIN users ON users.id = community_posts.owner_id
            WHERE users.public_profile = 1
            ORDER BY community_posts.created_at DESC, community_posts.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return {
        "success": True,
        "posts": [
            {
                "id": row["id"],
                "body": row["body"],
                "created_at": row["created_at"],
                "author": {
                    "display_name": row["display_name"],
                    "profile_handle": row["profile_handle"],
                },
            }
            for row in rows
        ],
        "notice": "The feed contains only updates real collectors chose to publish from public Cardr profiles.",
    }


@router.post("/api/community/posts")
async def create_community_post(data: Dict[str, Any], request: Request) -> Dict[str, Any]:
    user = require_user(request)
    if not user.get("public_profile"):
        raise HTTPException(
            status_code=403,
            detail="Enable your public profile before sharing a collector update.",
        )
    try:
        body = _clean_post(data.get("body"))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))
    post_id = uuid.uuid4().hex
    now = _now()
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO community_posts (id, owner_id, body, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (post_id, user["id"], body, now, now),
        )
        connection.commit()
    return {
        "success": True,
        "post": {
            "id": post_id,
            "body": body,
            "created_at": now,
            "author": {"display_name": user["display_name"], "profile_handle": user["profile_handle"]},
        },
    }


@router.get("/api/community/mine")
async def my_community_posts(request: Request) -> Dict[str, Any]:
    user = require_user(request)
    initialize_accounts()
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, body, created_at FROM community_posts
            WHERE owner_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 30
            """,
            (user["id"],),
        ).fetchall()
    return {
        "success": True,
        "posts": [
            {"id": row["id"], "body": row["body"], "created_at": row["created_at"]}
            for row in rows
        ],
    }


@router.delete("/api/community/posts/{post_id}")
async def delete_community_post(post_id: str, request: Request) -> Dict[str, Any]:
    user = require_user(request)
    with get_connection() as connection:
        cursor = connection.execute(
            "DELETE FROM community_posts WHERE id = ? AND owner_id = ?", (post_id, user["id"])
        )
        connection.commit()
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Collector update not found.")
    return {"success": True}
