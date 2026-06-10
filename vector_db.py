"""
vector_db.py — Daily vector index for Pinnacle team names.

Builds once per day from Pinnacle /events data. Each Pinnacle event
contributes two vectors (home + away team name), enabling cross-lingual
search so Hebrew Winner names can be matched to English Pinnacle names.

Embedding model: paraphrase-multilingual-mpnet-base-v2 (local, free,
handles Hebrew phonetic transliterations without any extra API calls).

Search uses numpy dot product on normalized vectors (cosine similarity).
At ~2000 daily team entries this is fast enough without a vector index server.
"""

import json
import logging
import pathlib
from datetime import datetime, timezone

import numpy as np
from sentence_transformers import SentenceTransformer

log = logging.getLogger(__name__)

_MODEL_NAME = "paraphrase-multilingual-mpnet-base-v2"
_STORE_DIR  = pathlib.Path(__file__).parent / "data" / "vector_db"


class DailyVectorDB:
    """
    In-memory vector store for one day's Pinnacle events.

    Typical lifecycle:
        db = DailyVectorDB()
        if not db.load("2026-06-10"):
            db.build(events)
            db.save("2026-06-10")
        results = db.search("ליברפול", "Soccer")
    """

    def __init__(self) -> None:
        self._model: SentenceTransformer | None = None
        self._embeddings: np.ndarray | None     = None
        self._metadata: list[dict]              = []

    # ── Model ─────────────────────────────────────────────────────────────────

    def _get_model(self) -> SentenceTransformer:
        if self._model is None:
            log.info("[VectorDB] Loading embedding model %s …", _MODEL_NAME)
            self._model = SentenceTransformer(_MODEL_NAME)
        return self._model

    # ── Build ─────────────────────────────────────────────────────────────────

    def build(self, events: list[dict]) -> None:
        """
        Embed all team names from a list of Pinnacle events and build the index.

        Each event produces two entries (home + away). Metadata stored per entry:
            event_id, sport_key, group, home_team, away_team, commence_time,
            team (the embedded name), role ("home" | "away")
        """
        entries: list[dict] = []
        for ev in events:
            for role, team in [("home", ev["home_team"]), ("away", ev["away_team"])]:
                if team:
                    entries.append({
                        "event_id":      ev["id"],
                        "sport_key":     ev["sport_key"],
                        "group":         ev["group"],
                        "home_team":     ev["home_team"],
                        "away_team":     ev["away_team"],
                        "commence_time": ev["commence_time"],
                        "team":          team,
                        "role":          role,
                    })

        if not entries:
            log.warning("[VectorDB] build() called with 0 events — index is empty.")
            return

        texts = [e["team"] for e in entries]
        log.info("[VectorDB] Embedding %d team names …", len(texts))
        embs = self._get_model().encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=False,
            batch_size=256,
        )
        self._embeddings = np.array(embs, dtype="float32")
        self._metadata   = entries
        log.info("[VectorDB] Built index — %d vectors, dim=%d",
                 len(entries), self._embeddings.shape[1])

    # ── Persist ───────────────────────────────────────────────────────────────

    def save(self, date_str: str) -> None:
        """Persist embeddings + metadata to data/vector_db/{date_str}.*"""
        if self._embeddings is None:
            log.error("[VectorDB] Nothing to save — build() first.")
            return

        _STORE_DIR.mkdir(parents=True, exist_ok=True)
        np.save(str(_STORE_DIR / f"{date_str}.npy"), self._embeddings)
        with open(_STORE_DIR / f"{date_str}.json", "w", encoding="utf-8") as f:
            json.dump(self._metadata, f, ensure_ascii=False, indent=None)

        log.info("[VectorDB] Saved %d entries for %s", len(self._metadata), date_str)

    def load(self, date_str: str) -> bool:
        """
        Load previously saved embeddings + metadata from disk.
        Returns True if both files exist and load succeeds.
        """
        npy_path  = _STORE_DIR / f"{date_str}.npy"
        json_path = _STORE_DIR / f"{date_str}.json"

        if not (npy_path.exists() and json_path.exists()):
            log.info("[VectorDB] No saved index for %s", date_str)
            return False

        try:
            self._embeddings = np.load(str(npy_path))
            with open(json_path, encoding="utf-8") as f:
                self._metadata = json.load(f)
        except Exception as exc:
            log.error("[VectorDB] Failed to load index for %s: %s", date_str, exc)
            return False

        log.info("[VectorDB] Loaded %d entries for %s", len(self._metadata), date_str)
        return True

    # ── Search ────────────────────────────────────────────────────────────────

    def search(self, query: str, group: str, top_k: int = 5) -> list[dict]:
        """
        Find the top-k Pinnacle team entries closest to query in the given group.

        Filters by Odds API group (e.g. "Soccer") before scoring, so a
        Hebrew football name is never confused with a tennis or basketball name.

        Returns list of metadata dicts each with an added 'score' float (cosine
        similarity, 1.0 = identical). Ordered by descending score.
        """
        if self._embeddings is None:
            log.error("[VectorDB] Index not loaded — call build() or load() first.")
            return []

        candidate_indices = [
            i for i, m in enumerate(self._metadata) if m["group"] == group
        ]
        if not candidate_indices:
            log.debug("[VectorDB] No entries for group %r", group)
            return []

        query_vec = self._get_model().encode([query], normalize_embeddings=True)
        query_vec = np.array(query_vec, dtype="float32")  # (1, D)

        candidate_embs = self._embeddings[candidate_indices]      # (N, D)
        scores         = (candidate_embs @ query_vec.T).flatten() # (N,)

        n      = min(top_k, len(scores))
        top_ix = scores.argsort()[::-1][:n]

        return [
            {**self._metadata[candidate_indices[i]], "score": float(scores[i])}
            for i in top_ix
        ]

    def get_by_time(
        self, group: str | None, kickoff: datetime, window_s: int = 900
    ) -> list[dict]:
        """
        Return all home-role Pinnacle entries that fall within window_s seconds
        of kickoff. When group is given, restricts to that Odds API sport group;
        when None, searches across all groups (used for unknown Winner sport IDs).

        Returns one dict per Pinnacle event (role="home" entries only, so each
        game appears once). Each dict has the full event metadata.
        """
        results = []
        for m in self._metadata:
            if m["role"] != "home":
                continue
            if group is not None and m["group"] != group:
                continue
            try:
                ct = datetime.fromisoformat(m["commence_time"])
                if ct.tzinfo is None:
                    ct = ct.replace(tzinfo=timezone.utc)
                if abs((ct - kickoff).total_seconds()) <= window_s:
                    results.append(m)
            except ValueError:
                continue
        return results