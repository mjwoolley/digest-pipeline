"""Tests for digest_pipeline.cluster — pure functions, no mocking."""
from digest_pipeline.cluster import cosine_similarity, cluster_articles, embedding_text


# ── cosine_similarity ────────────────────────────────────────────────────────

def test_cosine_identical_vectors():
    v = [1.0, 2.0, 3.0]
    assert cosine_similarity(v, v) == 1.0


def test_cosine_orthogonal_vectors():
    a = [1.0, 0.0]
    b = [0.0, 1.0]
    assert cosine_similarity(a, b) == 0.0


def test_cosine_zero_vector():
    assert cosine_similarity([0.0, 0.0], [1.0, 2.0]) == 0.0
    assert cosine_similarity([1.0, 2.0], [0.0, 0.0]) == 0.0


def test_cosine_opposite_vectors():
    a = [1.0, 0.0]
    b = [-1.0, 0.0]
    assert cosine_similarity(a, b) == -1.0


def test_cosine_similar_vectors():
    a = [1.0, 1.0]
    b = [1.0, 1.1]
    sim = cosine_similarity(a, b)
    assert 0.99 < sim < 1.0


# ── cluster_articles ─────────────────────────────────────────────────────────

def test_cluster_empty_input():
    assert cluster_articles([], []) == []


def test_cluster_identical_embeddings():
    articles = [{"title": "A"}, {"title": "B"}, {"title": "C"}]
    emb = [1.0, 0.0, 0.0]
    embeddings = [emb, emb, emb]
    clusters = cluster_articles(articles, embeddings)
    assert len(clusters) == 1
    assert len(clusters[0]) == 3


def test_cluster_distinct_embeddings():
    articles = [{"title": "A"}, {"title": "B"}]
    embeddings = [[1.0, 0.0], [0.0, 1.0]]  # orthogonal
    clusters = cluster_articles(articles, embeddings)
    assert len(clusters) == 2
    assert all(len(c) == 1 for c in clusters)


def test_cluster_threshold_boundary():
    articles = [{"title": "A"}, {"title": "B"}]
    # Two very similar vectors
    embeddings = [[1.0, 0.0], [0.99, 0.14]]  # cosine ~ 0.99
    clusters_high = cluster_articles(articles, embeddings, threshold=0.5)
    assert len(clusters_high) == 1  # should cluster

    clusters_strict = cluster_articles(articles, embeddings, threshold=0.999)
    assert len(clusters_strict) == 2  # too strict, separate


def test_cluster_sorted_by_size():
    articles = [{"title": "A"}, {"title": "B"}, {"title": "C"}]
    # B and C identical, A is different
    embeddings = [[1.0, 0.0], [0.0, 1.0], [0.0, 1.0]]
    clusters = cluster_articles(articles, embeddings)
    assert len(clusters[0]) >= len(clusters[-1])


# ── embedding_text ───────────────────────────────────────────────────────────

def test_embedding_text_normal():
    article = {"title": "My Title", "description": "Some desc"}
    assert embedding_text(article) == "My Title. Some desc"


def test_embedding_text_missing_fields():
    assert embedding_text({}) == ""
    assert embedding_text({"title": "T"}) == "T"
    assert embedding_text({"description": "D"}) == "D"


def test_embedding_text_truncates_long_description():
    """The title must dominate the vector: only a short description prefix is
    embedded, so divergent body prose can't drag same-story pairs apart."""
    article = {"title": "Launch", "description": "x" * 5000}
    text = embedding_text(article)
    assert len(text) <= len("Launch. ") + 300


def test_embedding_text_none_fields():
    assert embedding_text({"title": None, "description": None}) == ""
