"""SQLite 存储层。

去重策略是两层：
  1. 主键 (platform, post_id) —— 同一平台的同一条内容
  2. (platform, content_hash) —— 同平台内 post_id 变了但正文一样的情况
     （抖音换视频链接、公众号重新推送都会触发）

跨平台去重故意不做：同一条飞盘赛事新闻在公众号和小红书都出现时，
两个平台的互动数据、受众反馈都不一样，报告里应该分开呈现。
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Post

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    platform        TEXT    NOT NULL,
    post_id         TEXT    NOT NULL,
    title           TEXT    DEFAULT '',
    content         TEXT    DEFAULT '',
    author          TEXT    DEFAULT '',
    author_id       TEXT    DEFAULT '',
    url             TEXT    DEFAULT '',
    publish_ts      INTEGER,
    likes           INTEGER DEFAULT 0,
    comments        INTEGER DEFAULT 0,
    shares          INTEGER DEFAULT 0,
    collects        INTEGER DEFAULT 0,
    views           INTEGER DEFAULT 0,
    images          TEXT    DEFAULT '[]',
    videos          TEXT    DEFAULT '[]',
    collected_ts    INTEGER,
    matched_by      TEXT    DEFAULT '',
    relevance       REAL    DEFAULT 0.0,
    relevance_hits  TEXT    DEFAULT '[]',
    content_hash    TEXT    DEFAULT '',
    raw             TEXT    DEFAULT '{}',
    first_seen_ts   INTEGER,
    -- 首次被写进某份简报的时间。NULL = 还没报送过，watch 据此判断有没有新东西
    reported_ts     INTEGER,
    -- 链接可用性：'' 未查 / ok / dead / unknown，见 links.py。
    -- 公众号链接会过期，网页上据此提示读者。
    link_status     TEXT    DEFAULT '',
    link_checked_ts INTEGER,
    PRIMARY KEY (platform, post_id)
);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_ts   INTEGER,
    finished_ts  INTEGER,
    source       TEXT,
    target       TEXT,
    fetched      INTEGER DEFAULT 0,
    inserted     INTEGER DEFAULT 0,
    kept         INTEGER DEFAULT 0,
    status       TEXT,
    message      TEXT
);
"""

# 索引单独建：老库里可能还没有 migration 补上的列，
# 索引必须先等 _migrate() 跑完，否则 CREATE INDEX 会因为缺列直接报错。
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_posts_publish   ON posts(publish_ts DESC);
CREATE INDEX IF NOT EXISTS idx_posts_hash      ON posts(platform, content_hash);
CREATE INDEX IF NOT EXISTS idx_posts_relevance ON posts(relevance DESC);
CREATE INDEX IF NOT EXISTS idx_posts_author    ON posts(platform, author);
CREATE INDEX IF NOT EXISTS idx_posts_reported  ON posts(reported_ts);
"""

# 允许 from_row 通过 row["col"] 取值
_ROW_FACTORY = sqlite3.Row


class Storage:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = _ROW_FACTORY
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.executescript(INDEXES)
        self.conn.commit()

    def _migrate(self) -> None:
        """给已存在的老库补新列。

        CREATE TABLE IF NOT EXISTS 不会给已有表加列，所以增量字段必须
        单独 ALTER。每次加列都在这里补一条。
        """
        existing = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(posts)")
        }
        additions = {
            "reported_ts": "ALTER TABLE posts ADD COLUMN reported_ts INTEGER",
            "first_seen_ts": "ALTER TABLE posts ADD COLUMN first_seen_ts INTEGER",
            "link_status": "ALTER TABLE posts ADD COLUMN link_status TEXT DEFAULT ''",
            "link_checked_ts": "ALTER TABLE posts ADD COLUMN link_checked_ts INTEGER",
        }
        for column, ddl in additions.items():
            if column not in existing:
                self.conn.execute(ddl)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- 写入

    def upsert_many(self, posts: Iterable[Post]) -> tuple[int, int, int]:
        """写入一批内容，返回 (新增, 更新, 跳过重复)。

        已存在的记录会刷新互动数据（点赞/评论会随时间涨），但保留
        relevance 和 first_seen_ts 等首次采集的判定结果。
        """
        inserted = updated = skipped = 0
        seen_hashes: set[str] = set()

        for post in posts:
            if not post.is_valid():
                skipped += 1
                continue

            # 同批次内先去重（短文案不参与指纹去重，理由见 Post.is_hash_distinctive）
            if post.is_hash_distinctive:
                key = f"{post.platform}:{post.content_hash}"
                if key in seen_hashes:
                    skipped += 1
                    continue
                seen_hashes.add(key)

            existing = self.conn.execute(
                "SELECT post_id FROM posts WHERE platform=? AND post_id=?",
                (post.platform, post.post_id),
            ).fetchone()

            if existing:
                self._refresh(post)
                updated += 1
                continue

            # 正文指纹命中的话认为是同一条内容换了 id；
            # 短文案不算，见 Post.is_hash_distinctive
            dup = self.conn.execute(
                "SELECT post_id FROM posts WHERE platform=? AND content_hash=? LIMIT 1",
                (post.platform, post.content_hash),
            ).fetchone()
            if dup and post.is_hash_distinctive:
                skipped += 1
                continue

            self._insert(post)
            inserted += 1

        self.conn.commit()
        return inserted, updated, skipped

    def _insert(self, post: Post) -> None:
        row = post.to_row()
        now = int(datetime.now(timezone.utc).timestamp())
        row["first_seen_ts"] = now
        cols = ", ".join(row.keys())
        marks = ", ".join("?" for _ in row)
        self.conn.execute(
            f"INSERT INTO posts ({cols}) VALUES ({marks})", list(row.values())
        )

    def _refresh(self, post: Post) -> None:
        """已存在的内容只更新会变化的字段。

        publish_ts 用 COALESCE 包一层：平台的发布时间有时是后补的
        （比如小红书搜索接口只在角落标签里给月日，解析逻辑是后来加的），
        所以要允许回填；但某次解析失败时为 None，不能反过来把已知时间抹掉。
        """
        row = post.to_row()
        self.conn.execute(
            """UPDATE posts SET
                   likes=?, comments=?, shares=?, collects=?, views=?,
                   url=?, images=?, videos=?, collected_ts=?, raw=?,
                   publish_ts = COALESCE(?, publish_ts),
                   title = CASE WHEN ? != '' THEN ? ELSE title END,
                   author = CASE WHEN ? != '' THEN ? ELSE author END
               WHERE platform=? AND post_id=?""",
            (
                post.likes, post.comments, post.shares, post.collects, post.views,
                post.url,
                json.dumps(post.images, ensure_ascii=False),
                json.dumps(post.videos, ensure_ascii=False),
                int(datetime.now(timezone.utc).timestamp()),
                json.dumps(post.raw, ensure_ascii=False, default=str),
                row["publish_ts"],
                # 标题/作者也允许补：平台改版时字段可能先是空的，后来才取到
                post.title, post.title,
                post.author, post.author,
                post.platform, post.post_id,
            ),
        )

    # ---------------------------------------------------------------- 查询

    def query(
        self,
        platforms: Sequence[str] | None = None,
        since: datetime | None = None,
        min_relevance: float = 0.0,
        limit: int = 500,
        ascending: bool = False,
    ) -> list[Post]:
        sql = "SELECT * FROM posts WHERE relevance >= ?"
        params: list[object] = [min_relevance]

        if platforms:
            marks = ", ".join("?" for _ in platforms)
            sql += f" AND platform IN ({marks})"
            params.extend(platforms)

        if since is not None:
            sql += " AND COALESCE(publish_ts, collected_ts) >= ?"
            params.append(int(since.timestamp()))

        # 时间缺失的（部分平台不给发布时间）排到末尾而不是丢掉
        direction = "ASC" if ascending else "DESC"
        sql += f" ORDER BY COALESCE(publish_ts, collected_ts) {direction} LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(sql, params).fetchall()
        return [Post.from_row(r) for r in rows]

    def recent(self, days: int = 7, **kwargs: object) -> list[Post]:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        return self.query(since=since, **kwargs)  # type: ignore[arg-type]

    # ------------------------------------------------------- 增量报送追踪

    def unreported(
        self,
        min_relevance: float = 0.5,
        platforms: Sequence[str] | None = None,
        limit: int = 500,
    ) -> list[Post]:
        """取出还没被写进任何简报的内容。

        「有新东西」的判定就是这个：库里存在 reported_ts 为空的相关内容。
        每条内容只会进一份简报，不会在后续简报里重复出现。
        """
        sql = "SELECT * FROM posts WHERE relevance >= ? AND reported_ts IS NULL"
        params: list[object] = [min_relevance]

        if platforms:
            marks = ", ".join("?" for _ in platforms)
            sql += f" AND platform IN ({marks})"
            params.extend(platforms)

        sql += " ORDER BY relevance DESC, COALESCE(publish_ts, collected_ts) DESC LIMIT ?"
        params.append(limit)
        return [Post.from_row(r) for r in self.conn.execute(sql, params).fetchall()]

    def count_unreported(
        self,
        min_relevance: float = 0.5,
        authors: Sequence[str] | None = None,
    ) -> int:
        """统计未报送条数。传 authors 时只数这些账号的（用于权威号判定）。"""
        sql = "SELECT COUNT(*) AS n FROM posts WHERE relevance >= ? AND reported_ts IS NULL"
        params: list[object] = [min_relevance]

        if authors:
            marks = ", ".join("?" for _ in authors)
            sql += f" AND author IN ({marks})"
            params.extend(authors)

        return int(self.conn.execute(sql, params).fetchone()["n"])

    def unreported_from_authors(
        self,
        authors: Sequence[str],
        min_relevance: float = 0.5,
        limit: int = 200,
    ) -> list[Post]:
        """取指定账号（权威号）的未报送内容。"""
        if not authors:
            return []
        marks = ", ".join("?" for _ in authors)
        rows = self.conn.execute(
            f"""SELECT * FROM posts
                WHERE relevance >= ? AND reported_ts IS NULL AND author IN ({marks})
                ORDER BY COALESCE(publish_ts, collected_ts) DESC LIMIT ?""",
            [min_relevance, *authors, limit],
        ).fetchall()
        return [Post.from_row(r) for r in rows]

    def mark_reported(self, posts: Sequence[Post]) -> int:
        """把内容标记为已报送。写完简报后调用，成功即不再重复产出。"""
        if not posts:
            return 0
        now = int(datetime.now(timezone.utc).timestamp())
        self.conn.executemany(
            "UPDATE posts SET reported_ts=? WHERE platform=? AND post_id=?",
            [(now, p.platform, p.post_id) for p in posts],
        )
        self.conn.commit()
        return len(posts)

    def mark_all_reported_before(self, moment: datetime | None = None) -> int:
        """把某时刻之前的全部内容标记为已报送，用来立一个基线。

        首次启用增量模式时用：否则历史积累的几百条会在第一份简报里
        全部涌出来。
        """
        cutoff = int((moment or datetime.now(timezone.utc)).timestamp())
        cur = self.conn.execute(
            "UPDATE posts SET reported_ts=? WHERE reported_ts IS NULL AND COALESCE(collected_ts, 0) <= ?",
            (cutoff, cutoff),
        )
        self.conn.commit()
        return cur.rowcount

    def newest_publish_time(self) -> datetime | None:
        row = self.conn.execute(
            "SELECT MAX(COALESCE(publish_ts, collected_ts)) AS ts FROM posts"
        ).fetchone()
        return (
            datetime.fromtimestamp(row["ts"], tz=timezone.utc)
            if row and row["ts"]
            else None
        )

    def last_reported_at(self) -> datetime | None:
        """上一份简报的生成时间。

        不用单独的 state 文件：reported_ts 的最大值就是它，少一处可能
        和数据库不一致的外部状态。
        """
        row = self.conn.execute(
            "SELECT MAX(reported_ts) AS ts FROM posts"
        ).fetchone()
        return (
            datetime.fromtimestamp(row["ts"], tz=timezone.utc)
            if row and row["ts"]
            else None
        )

    # -------------------------------------------------------- 链接可用性

    def posts_needing_link_check(
        self, hours: int = 24, platforms: Sequence[str] = ("wechat",), limit: int = 400
    ) -> list[Post]:
        """取出需要检查链接的内容。

        只查指定平台（目前只有公众号会过期），并且跳过 `hours` 小时内
        刚查过的 —— 查得太勤是白打平台，也拖慢每天那一轮。

        `hours <= 0` 表示**强制重查**，不管什么时候查过。
        这是个显式的「忽略缓存」模式：不能靠「时间戳小于当前时间」来表达，
        因为时间戳存的是整秒，同一秒内比较不出来，会静默变成「全部跳过」。
        """
        marks = ", ".join("?" for _ in platforms)
        params: list[object] = [*platforms]

        sql = f"""SELECT * FROM posts
                  WHERE platform IN ({marks}) AND url != ''"""
        if hours > 0:
            cutoff = int(
                (datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp()
            )
            sql += " AND (link_checked_ts IS NULL OR link_checked_ts < ?)"
            params.append(cutoff)

        sql += " ORDER BY COALESCE(collected_ts, 0) DESC LIMIT ?"
        params.append(limit)

        return [Post.from_row(r) for r in self.conn.execute(sql, params).fetchall()]

    def update_link_status(self, checks: Sequence[object]) -> int:
        """写入链接检查结果。checks 里每项要有 platform/post_id/status。"""
        now = int(datetime.now(timezone.utc).timestamp())
        rows = [
            (getattr(c, "status", ""), now, getattr(c, "platform", ""),
             getattr(c, "post_id", ""))
            for c in checks
        ]
        self.conn.executemany(
            "UPDATE posts SET link_status=?, link_checked_ts=? "
            "WHERE platform=? AND post_id=?",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def link_stats(self) -> dict[str, int]:
        """各状态的链接数量，用于发布时报告。"""
        out: dict[str, int] = {}
        for row in self.conn.execute(
            """SELECT COALESCE(NULLIF(link_status, ''), 'unchecked') AS st,
                      COUNT(*) AS n
               FROM posts WHERE url != '' GROUP BY st"""
        ):
            out[row["st"]] = row["n"]
        return out

    def author_counts(self, limit: int = 200) -> list[tuple[str, int]]:
        rows = self.conn.execute(
            """SELECT author, COUNT(*) AS n FROM posts
               WHERE author != '' GROUP BY author ORDER BY n DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [(r["author"], r["n"]) for r in rows]

    def stats(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in self.conn.execute(
            "SELECT platform, COUNT(*) AS n FROM posts GROUP BY platform"
        ):
            out[row["platform"]] = row["n"]
        out["_total"] = sum(out.values())
        return out

    def run_stats(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM crawl_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    # ---------------------------------------------------------------- 采集日志

    def log_run(
        self,
        source: str,
        target: str,
        fetched: int,
        inserted: int,
        kept: int,
        status: str,
        message: str = "",
        started_ts: int | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO crawl_runs
                   (started_ts, finished_ts, source, target, fetched, inserted, kept, status, message)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                started_ts or int(datetime.now(timezone.utc).timestamp()),
                int(datetime.now(timezone.utc).timestamp()),
                source, target, fetched, inserted, kept, status, message[:500],
            ),
        )
        self.conn.commit()

    # ---------------------------------------------------------------- 导出

    def export_jsonl(self, posts: Sequence[Post], path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            for post in posts:
                fh.write(json.dumps(post.to_dict(), ensure_ascii=False) + "\n")
        return path

    def export_csv(self, posts: Sequence[Post], path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        cols = [
            "platform", "post_id", "title", "author", "url", "publish_time",
            "likes", "comments", "shares", "collects", "views",
            "relevance", "matched_by", "content",
        ]
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            writer.writeheader()
            for post in posts:
                row = post.to_dict()
                # CSV 里正文压成一行并截断，否则一个换行就把表结构撑坏
                row["content"] = (post.content or "").replace("\n", " ")[:200]
                writer.writerow(row)
        return path
