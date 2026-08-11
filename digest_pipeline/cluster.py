"""Embedding-based article clustering using cosine similarity.
Pure Python — no external dependencies.
"""
import math


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def cluster_articles(articles: list[dict], embeddings: list[list[float]],
                     threshold: float = 0.85) -> list[list[dict]]:
    """Cluster articles by embedding similarity.

    Algorithm:
    1. For each article, check similarity against existing cluster centroids
    2. Join the first cluster where similarity > threshold
    3. Articles that don't match any cluster become their own cluster
    4. Return clusters sorted by size (largest first)

    Returns list of clusters (each cluster is a list of articles).
    Single-article clusters = unique stories.
    Multi-article clusters = duplicates to merge.
    """
    if not articles:
        return []

    clusters: list[list[int]] = []          # indices into articles
    cluster_embeds: list[list[float]] = []  # centroid of each cluster

    for i, emb in enumerate(embeddings):
        placed = False
        for ci, centroid in enumerate(cluster_embeds):
            if cosine_similarity(emb, centroid) > threshold:
                clusters[ci].append(i)
                # Update centroid as running average
                n = len(clusters[ci])
                cluster_embeds[ci] = [
                    (c * (n - 1) + e) / n
                    for c, e in zip(centroid, emb)
                ]
                placed = True
                break
        if not placed:
            clusters.append([i])
            cluster_embeds.append(list(emb))

    # Convert indices to article dicts, sort by cluster size descending
    result = []
    for idx_list in clusters:
        result.append([articles[i] for i in idx_list])
    result.sort(key=len, reverse=True)
    return result


def embedding_text(article: dict, desc_chars: int = 300) -> str:
    """Build the text to embed for an article.

    The title is the identity signal: two outlets covering the same story
    share headline vocabulary but diverge in body prose, so embedding the
    full verbatim description let the divergent body dominate the vector
    and dragged real duplicates below the similarity threshold. Only a
    short description prefix is included for disambiguation.
    """
    title = (article.get("title") or "").strip()
    desc = (article.get("description") or "").strip()[:desc_chars]
    if title and desc:
        return f"{title}. {desc}"
    return title or desc
