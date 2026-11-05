"""Full-text search with PostgreSQL.

How it works:
- Each searchable table has a `search_vector` column (type tsvector) that
  Postgres computes from the text fields whenever a row is written. It holds
  the *stems* of the words ("restarting" -> "restart") with a weight: A for the
  title, B for the summary, C for the body. Common words ("the", "and") are
  dropped.
- A GIN index on that column makes "which rows contain these words?" fast.
- `websearch_to_tsquery` turns what someone typed into a query, using the
  same rules search engines do: `dns restart` (both words), `"exact phrase"`,
  `dns or dhcp`, `-vpn` (without). It never fails on odd input.
- `ts_rank` scores each match, so the best ones can be listed first (a title
  match beats a body match, thanks to the weights).

Ask (app/ask.py) uses the same columns, but with `any_word_matches`: a
question is full sentences, and a record rarely contains *every* word of one.
"""

import re

from sqlalchemy import ColumnElement, func

LANGUAGE = "english"  # how words are stemmed; also used in app/models.py
QUERY_MAX = 200  # longer search text is cut off
MAX_WORDS = 30  # Ask looks for at most this many words of a question


def search_vector_sql(**weighted_columns: str) -> str:
    """The SQL that computes a search_vector column, e.g.
    search_vector_sql(title="A", summary="B") ->
    "setweight(to_tsvector('english', coalesce(title, '')), 'A') || ..."
    """
    return " || ".join(
        f"setweight(to_tsvector('{LANGUAGE}', coalesce({column}, '')), '{weight}')"
        for column, weight in weighted_columns.items()
    )


def tidy_query(text: str | None) -> str:
    """What was typed in the search box, trimmed; '' means "no search"."""
    return (text or "").strip()[:QUERY_MAX]


def matches(search_vector, text: str) -> tuple[ColumnElement[bool], ColumnElement[float]]:
    """A WHERE condition ("matches the search") and a relevance score to sort by."""
    query = func.websearch_to_tsquery(LANGUAGE, text)
    return search_vector.op("@@")(query), func.ts_rank(search_vector, query)


def any_word_matches(
    search_vector, text: str
) -> tuple[ColumnElement[bool], ColumnElement[float]] | None:
    """Like `matches`, but a record matches if it has ANY of the words; records
    with more (and rarer, title) matches rank higher. None if there are no words.

    Only letters and digits are kept, so nothing typed can change the query's
    meaning (`to_tsquery` treats characters like & | ! : as operators).
    Common words ("how", "the") are dropped by Postgres itself.
    """
    words = re.findall(r"[^\W_]+", text)[:MAX_WORDS]
    if not words:
        return None
    query = func.to_tsquery(LANGUAGE, " | ".join(words))
    return search_vector.op("@@")(query), func.ts_rank(search_vector, query)
