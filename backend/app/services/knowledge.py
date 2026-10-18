"""Small private document retrieval: chunking, two ranked routes, RRF, citations and abstention."""
from collections import Counter
from datetime import date
import math
import re
from uuid import uuid4

from fastapi import HTTPException

from . import store


def terms(text):
    words = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", text.lower())
    return [token for word in words for token in ([word] if len(word) < 2 or word.isascii()
                                                else [word[i:i+2] for i in range(len(word)-1)])]


def variants(query):
    rewritten = query
    for source, target in {"退钱": "退款", "约票": "预约", "几点开门": "开放时间", "轮椅": "无障碍"}.items():
        rewritten = rewritten.replace(source, target)
    return list(dict.fromkeys([query, rewritten]))


def add_document(user_id, document):
    if document.valid_until:
        try:
            date.fromisoformat(document.valid_until)
        except ValueError as exc:
            raise HTTPException(422, "valid_until 必须是有效日期") from exc
    identifier = uuid4().hex
    with store._connect() as db:
        if db.execute("SELECT COUNT(*) FROM knowledge_document WHERE user_id=?", (user_id,)).fetchone()[0] >= 200:
            raise HTTPException(409, "每位用户最多保存 200 份资料，请先删除旧资料")
        db.execute("INSERT INTO knowledge_document VALUES(?,?,?,?,?,?,?)",
                   (identifier, user_id, document.title, document.source, document.content, document.valid_until, store._now()))
    return identifier


def retrieve(user_id, query, top_k=5):
    with store._connect() as db:
        rows = db.execute("SELECT * FROM knowledge_document WHERE user_id=? AND (valid_until IS NULL OR valid_until>=?)",
                          (user_id, date.today().isoformat())).fetchall()
    chunks = []
    for row in rows:
        content = row["content"]
        for offset in range(0, len(content), 500):
            text = content[max(0, offset-80):offset+500]
            chunks.append({"chunk_id": f"{row['document_id']}:{offset}", "document_id": row["document_id"],
                           "title": row["title"], "source": row["source"], "content": text,
                           "updated_at": row["updated_at"], "valid_until": row["valid_until"],
                           "tokens": Counter(terms(text)), "title_tokens": set(terms(row["title"]))})
    if not chunks:
        return [], {"candidate_count": 0, "returned_count": 0, "variants": variants(query), "reason": "no_valid_documents"}
    frequency = Counter(token for chunk in chunks for token in chunk["tokens"])
    average = sum(sum(c["tokens"].values()) for c in chunks) / len(chunks)
    fused = Counter()
    for variant in variants(query):
        tokens = set(terms(variant))
        lexical, title = [], []
        for index, chunk in enumerate(chunks):
            length = sum(chunk["tokens"].values())
            score = 0.0
            for token in tokens:
                count = chunk["tokens"][token]
                if count:
                    inverse = math.log(1 + (len(chunks)-frequency[token]+0.5)/(frequency[token]+0.5))
                    score += inverse * count * 2.2 / (count + 1.2 * (0.25 + 0.75 * length/max(average, 1)))
            if score:
                lexical.append((score, index))
            overlap = len(tokens & chunk["title_tokens"])
            if overlap:
                title.append((overlap, index))
        for ranking in (lexical, title):
            for rank, (_, index) in enumerate(sorted(ranking, reverse=True)[:30], 1):
                fused[index] += 1 / (60 + rank)
    # Rerank only already-recalled candidates; don't mix raw BM25/title score scales.
    tokens = set(terms(" ".join(variants(query))))
    def relevance(index):
        chunk = chunks[index]
        overlap = len(tokens & (set(chunk["tokens"]) | chunk["title_tokens"])) / max(len(tokens), 1)
        return overlap, fused[index]
    ranked = sorted(fused, key=relevance, reverse=True)
    selected = [chunks[i] for i in ranked[:top_k] if relevance(i)[0] >= 0.15]
    return [{key: value for key, value in chunk.items() if key not in ("tokens", "title_tokens")} for chunk in selected], {
        "candidate_count": len(fused), "returned_count": len(selected), "variants": variants(query),
        "reason": "matched" if selected else ("low_overlap" if fused else "no_overlap"), "fusion": "rrf", "rank_constant": 60}


def answer(user_id, query):
    documents, _ = retrieve(user_id, query)
    if not documents:
        return "没有检索到可用资料，无法确认这项规则。请添加官方资料或向场所核实。", []
    # Extractive answer: quotations keep source conflicts visible and prevent fabricated rules.
    reply = "找到以下资料，内容可能存在不同适用范围，请核对来源和有效期：\n" + "\n".join(
        f"[{index}] {doc['title']}：{doc['content']}" for index, doc in enumerate(documents, 1))
    return reply, documents
