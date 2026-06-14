import hashlib
import json
import math
import re
import time
from pathlib import Path

import requests
from flask import current_app

from .attachment_text import attachment_path, attachment_text_context, extract_attachment_text
from .models import Attachment, MaterialChunk, MaterialDocument, MaterialRetrievalLog, db


SUPPORTED_RAG_FILE_TYPES = {"pptx", "docx", "xlsx", "pdf"}
LANCEDB_TABLE_NAME = "material_chunks"
SentenceTransformer = None
LOCAL_EMBEDDING_CLIENTS = {}


class LocalMiniLMEmbeddingClient:
    def __init__(self, model_path, batch_size=16):
        self.model_path = Path(model_path)
        self.batch_size = max(1, int(batch_size or 16))
        self.model = str(self.model_path)
        self._backend = None
        self._session = None
        self._tokenizer = None
        self._sentence_model = None

    def embed_texts(self, texts):
        texts = list(texts)
        if not texts:
            return []
        self._ensure_loaded()
        vectors = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            if self._backend == "onnx":
                vectors.extend(self._embed_with_onnx(batch))
            else:
                encoded = self._sentence_model.encode(
                    batch,
                    batch_size=self.batch_size,
                    normalize_embeddings=True,
                    show_progress_bar=False,
                )
                vectors.extend(encoded.tolist() if hasattr(encoded, "tolist") else encoded)
        return [list(map(float, vector)) for vector in vectors]

    def _ensure_loaded(self):
        if self._backend:
            return
        if not self.model_path.exists():
            raise RuntimeError(f"本地 embedding 模型目录不存在：{self.model_path}")
        try:
            self._load_onnx()
            self._backend = "onnx"
            return
        except Exception as onnx_exc:
            try:
                self._load_sentence_transformer()
                self._backend = "sentence_transformers"
                return
            except Exception as st_exc:
                raise RuntimeError(
                    f"本地 embedding 模型加载失败；ONNX: {onnx_exc}；SentenceTransformer: {st_exc}"
                ) from st_exc

    def _load_onnx(self):
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise RuntimeError("缺少 onnxruntime 或 tokenizers 依赖") from exc
        onnx_path = self.model_path / "onnx" / "model.onnx"
        tokenizer_path = self.model_path / "tokenizer.json"
        if not onnx_path.exists():
            raise RuntimeError(f"缺少 ONNX 模型文件：{onnx_path}")
        if not tokenizer_path.exists():
            raise RuntimeError(f"缺少 tokenizer 文件：{tokenizer_path}")
        self._session = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
        self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self._tokenizer.enable_truncation(max_length=256)
        self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")

    def _load_sentence_transformer(self):
        global SentenceTransformer
        if SentenceTransformer is None:
            try:
                from sentence_transformers import SentenceTransformer as ImportedSentenceTransformer
            except ImportError as exc:
                raise RuntimeError("缺少 sentence-transformers 依赖") from exc
            SentenceTransformer = ImportedSentenceTransformer
        self._sentence_model = SentenceTransformer(str(self.model_path))

    def _embed_with_onnx(self, texts):
        import numpy as np

        encodings = self._tokenizer.encode_batch([text or "" for text in texts])
        input_ids = np.array([encoding.ids for encoding in encodings], dtype=np.int64)
        attention_mask = np.array([encoding.attention_mask for encoding in encodings], dtype=np.int64)
        token_type_ids = np.array([encoding.type_ids for encoding in encodings], dtype=np.int64)
        candidate_inputs = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "token_type_ids": token_type_ids,
        }
        input_names = {item.name for item in self._session.get_inputs()}
        inputs = {name: value for name, value in candidate_inputs.items() if name in input_names}
        output_names = [item.name for item in self._session.get_outputs()]
        outputs = self._session.run(output_names, inputs)
        token_embeddings = outputs[0]
        return normalize_vectors(mean_pool(token_embeddings, attention_mask)).tolist()


class MaterialEmbeddingClient:
    def __init__(self, base_url, api_key, model="", timeout=60):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.timeout = timeout

    @property
    def headers(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def embed_texts(self, texts):
        payload = {"input": list(texts)}
        if self.model:
            payload["model"] = self.model
        response = requests.post(
            f"{self.base_url}/api/v1/embeddings",
            headers=self.headers,
            json=payload,
            timeout=self.timeout,
        )
        if response.status_code == 401:
            raise RuntimeError("智枢 embedding 接口鉴权失败，请检查 ZHISHU_API_KEY")
        if response.status_code >= 400:
            raise RuntimeError(f"{response.status_code} {response.reason}: {response.text[:500].strip()}")
        data = response.json()
        embeddings = parse_embedding_response(data)
        if len(embeddings) != len(texts):
            raise RuntimeError("智枢 embedding 返回数量与 chunk 数量不一致")
        return embeddings


class LanceDBVectorStore:
    def __init__(self, db_path):
        self.db_path = Path(db_path)

    def connect(self):
        try:
            import lancedb
        except ImportError as exc:
            raise RuntimeError("LanceDB 未安装，请确认 wheels 离线包和 requirements.txt") from exc
        self.db_path.mkdir(parents=True, exist_ok=True)
        return lancedb.connect(str(self.db_path))

    def open_table(self):
        database = self.connect()
        if LANCEDB_TABLE_NAME not in database.table_names():
            return None
        return database.open_table(LANCEDB_TABLE_NAME)

    def upsert_chunks(self, chunks, vectors):
        records = [lancedb_record_for_chunk(chunk, vector) for chunk, vector in zip(chunks, vectors)]
        if not records:
            return
        database = self.connect()
        if LANCEDB_TABLE_NAME not in database.table_names():
            database.create_table(LANCEDB_TABLE_NAME, data=records)
            return
        table = database.open_table(LANCEDB_TABLE_NAME)
        chunk_ids = ",".join(str(record["chunk_id"]) for record in records)
        table.delete(f"chunk_id IN ({chunk_ids})")
        table.add(records)

    def search(self, query_vector, top_k, scope_type, scope_id, visible_topic_ids=None):
        table = self.open_table()
        if table is None:
            return []
        if visible_topic_ids is not None and not visible_topic_ids:
            return []
        filter_expr = lancedb_filter(scope_type, scope_id, visible_topic_ids)
        query = table.search(query_vector).limit(top_k)
        if filter_expr:
            query = query.where(filter_expr, prefilter=True)
        return [
            {
                "chunk_id": int(item["chunk_id"]),
                "vector_score": lancedb_vector_score(item),
            }
            for item in query.to_list()
        ]

    def delete_document(self, document_id):
        table = self.open_table()
        if table is not None:
            table.delete(f"document_id = {int(document_id)}")


class ZhishuKnowledgeClient:
    def __init__(self, base_url, api_key, timeout=30, auth_header="Authorization"):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.timeout = timeout
        self.auth_header = auth_header or "Authorization"

    @property
    def headers(self):
        if self.auth_header.lower() == "authorization":
            return {"Authorization": f"Bearer {self.api_key}"}
        return {self.auth_header: self.api_key}

    @property
    def json_headers(self):
        return {**self.headers, "Content-Type": "application/json"}

    def ensure_knowledge_base(self, scope_type, scope_id, title):
        name = f"qbp-{scope_type}-{scope_id}"
        payload = {
            "name": name,
            "description": title,
        }
        response = requests.post(
            f"{self.base_url}/api/v1/knowledge/create",
            headers=self.json_headers,
            json=payload,
            timeout=self.timeout,
        )
        if response.status_code in {400, 409}:
            return name
        self.raise_for_status(response)
        data = response.json()
        return str(data.get("id") or data.get("collection_name") or data.get("name") or name)

    def upload_file(self, path, metadata=None):
        with Path(path).open("rb") as handle:
            response = requests.post(
                f"{self.base_url}/api/v1/files/",
                headers=self.headers,
                files={"file": (Path(path).name, handle)},
                timeout=self.timeout,
            )
        self.raise_for_status(response)
        data = response.json()
        return str(data.get("id") or data.get("file_id") or data.get("data", {}).get("id") or "")

    def add_file_to_knowledge(self, knowledge_id, file_id):
        response = requests.post(
            f"{self.base_url}/api/v1/knowledge/{knowledge_id}/file/add",
            headers=self.json_headers,
            json={"file_id": file_id},
            timeout=self.timeout,
        )
        self.raise_for_status(response)

    def wait_for_file_processed(self, file_id, timeout_seconds=60):
        deadline = time.monotonic() + max(1, int(timeout_seconds or 60))
        while time.monotonic() < deadline:
            response = requests.get(
                f"{self.base_url}/api/v1/files/{file_id}",
                headers=self.headers,
                timeout=self.timeout,
            )
            if response.status_code == 404:
                return True
            self.raise_for_status(response)
            data = response.json()
            status = str(data.get("status") or data.get("data", {}).get("status") or "").lower()
            if status in {"", "processed", "completed", "indexed", "ready"}:
                return True
            if status in {"failed", "error"}:
                raise RuntimeError(data.get("error") or "智枢文件处理失败")
            time.sleep(1)
        return False

    def raise_for_status(self, response):
        if response.status_code == 401:
            raise RuntimeError(
                "智枢知识库接口鉴权失败，请检查 ZHISHU_KNOWLEDGE_API_KEY；"
                "如果智枢网关不接收 Authorization Bearer，请设置 ZHISHU_KNOWLEDGE_AUTH_HEADER=X-API-Key"
            )
        if response.status_code >= 400:
            detail = response.text[:500].strip()
            raise RuntimeError(f"{response.status_code} {response.reason}: {detail}")
        response.raise_for_status()


def zhishu_knowledge_client():
    configured = current_app.config.get("ZHISHU_KNOWLEDGE_CLIENT")
    if configured:
        return configured
    if not current_app.config.get("ZHISHU_KNOWLEDGE_ENABLED", True):
        return None
    api_key = current_app.config.get("ZHISHU_KNOWLEDGE_API_KEY") or current_app.config.get("ZHISHU_API_KEY")
    if not api_key:
        return None
    return ZhishuKnowledgeClient(
        current_app.config.get("ZHISHU_KNOWLEDGE_BASE_URL") or current_app.config.get("ZHISHU_BASE_URL", ""),
        api_key,
        auth_header=current_app.config.get("ZHISHU_KNOWLEDGE_AUTH_HEADER", "Authorization"),
    )


def material_embedding_client():
    configured = current_app.config.get("MATERIAL_RAG_EMBEDDING_CLIENT")
    if configured:
        return configured
    if not current_app.config.get("MATERIAL_RAG_VECTOR_ENABLED", True):
        return None
    if current_app.config.get("MATERIAL_RAG_LOCAL_EMBEDDING_ENABLED", True):
        model_path = Path(current_app.config.get("MATERIAL_RAG_LOCAL_EMBEDDING_MODEL_PATH", ""))
        if model_path.exists():
            batch_size = int(current_app.config.get("MATERIAL_RAG_LOCAL_EMBEDDING_BATCH_SIZE", 16))
            cache_key = (str(model_path.resolve()), batch_size)
            if cache_key not in LOCAL_EMBEDDING_CLIENTS:
                LOCAL_EMBEDDING_CLIENTS[cache_key] = LocalMiniLMEmbeddingClient(model_path, batch_size=batch_size)
            return LOCAL_EMBEDDING_CLIENTS[cache_key]
    api_key = current_app.config.get("ZHISHU_API_KEY")
    if not api_key:
        return None
    return MaterialEmbeddingClient(
        current_app.config.get("ZHISHU_BASE_URL", ""),
        api_key,
        model=current_app.config.get("MATERIAL_RAG_EMBEDDING_MODEL", ""),
    )


def material_vector_store():
    configured = current_app.config.get("MATERIAL_RAG_VECTOR_STORE")
    if configured:
        return configured
    if not current_app.config.get("MATERIAL_RAG_VECTOR_ENABLED", True):
        return None
    return LanceDBVectorStore(current_app.config.get("MATERIAL_RAG_VECTOR_DB_PATH"))


def parse_embedding_response(data):
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return [item.get("embedding") for item in data["data"] if isinstance(item, dict)]
    if isinstance(data, dict) and isinstance(data.get("embeddings"), list):
        embeddings = data["embeddings"]
        if embeddings and isinstance(embeddings[0], dict):
            return [item.get("embedding") for item in embeddings]
        return embeddings
    if isinstance(data, dict) and isinstance(data.get("embedding"), list):
        return [data["embedding"]]
    return []


def lancedb_record_for_chunk(chunk, vector):
    return {
        "chunk_id": int(chunk.id),
        "document_id": int(chunk.document_id),
        "attachment_id": int(chunk.attachment_id),
        "topic_id": int(chunk.topic_id),
        "meeting_id": int(chunk.meeting_id or 0),
        "filename": chunk.attachment.original_filename if chunk.attachment else "",
        "source_label": chunk.source_label or f"Chunk {chunk.chunk_index}",
        "chunk_index": int(chunk.chunk_index),
        "text_hash": chunk.text_hash,
        "text": chunk.text,
        "vector": [float(value) for value in vector],
    }


def lancedb_filter(scope_type, scope_id, visible_topic_ids=None):
    if scope_type == "topic":
        return f"topic_id = {int(scope_id)}"
    parts = [f"meeting_id = {int(scope_id)}"]
    if visible_topic_ids is not None:
        ids = ",".join(str(int(topic_id)) for topic_id in visible_topic_ids)
        parts.append(f"topic_id IN ({ids})")
    return " AND ".join(parts)


def lancedb_vector_score(item):
    distance = item.get("_distance")
    if distance is None:
        return 1.0
    try:
        return 1.0 / (1.0 + float(distance))
    except (TypeError, ValueError):
        return 0.0


def mean_pool(token_embeddings, attention_mask):
    import numpy as np

    expanded_mask = np.expand_dims(attention_mask, axis=-1).astype(np.float32)
    masked_embeddings = token_embeddings * expanded_mask
    summed = masked_embeddings.sum(axis=1)
    counts = np.clip(expanded_mask.sum(axis=1), 1e-9, None)
    return summed / counts


def normalize_vectors(vectors):
    import numpy as np

    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.clip(norms, 1e-12, None)


def sanitized_zhishu_metadata(metadata):
    safe = {}
    for key, value in (metadata or {}).items():
        if value is None:
            safe[key] = ""
        elif isinstance(value, (str, int, float, bool)):
            safe[key] = value
        else:
            safe[key] = json.dumps(value, ensure_ascii=False)
    return safe


def index_attachment_material(attachment, file_path=None):
    document = ensure_material_document(attachment)
    document.status = "indexing"
    document.error_message = ""
    document.chunk_count = 0
    delete_document_vectors(document)
    MaterialChunk.query.filter_by(document_id=document.id).delete()
    db.session.flush()

    file_type = attachment.effective_file_type
    if file_type not in SUPPORTED_RAG_FILE_TYPES:
        document.status = "unsupported"
        document.error_message = "该附件类型暂不支持材料 RAG 索引。"
        db.session.commit()
        return document

    path = Path(file_path) if file_path else attachment_path(attachment)
    text, warning = extract_decrypted_attachment(path, file_type)
    if warning or not text.strip():
        document.status = "failed"
        document.error_message = warning or "附件未抽取到可索引正文。"
        db.session.commit()
        return document

    document.text_hash = sha256_text(text)
    chunks = chunk_material(
        text,
        current_app.config.get("MATERIAL_RAG_CHUNK_CHARS", 1400),
        current_app.config.get("MATERIAL_RAG_CHUNK_OVERLAP", 150),
    )
    chunk_models = []
    for chunk in chunks:
        chunk_model = MaterialChunk(
            document_id=document.id,
            attachment_id=attachment.id,
            topic_id=attachment.topic_id,
            meeting_id=attachment.topic.meeting_id,
            chunk_index=chunk["chunk_index"],
            source_label=chunk["source_label"],
            text=chunk["text"],
            text_hash=sha256_text(chunk["text"]),
            char_count=len(chunk["text"]),
        )
        chunk_models.append(chunk_model)
        db.session.add(chunk_model)
    document.chunk_count = len(chunks)
    db.session.flush()

    vector_error = index_chunk_vectors(chunk_models)

    try:
        sync_document_to_zhishu(document, path)
    except Exception as exc:
        current_app.logger.warning("Material indexing uploaded local chunks but Zhishu sync failed: %s", exc)
        document.status = "indexed"
        if known_zhishu_metadata_sync_error(exc):
            document.error_message = ""
        else:
            document.error_message = f"本地索引可用；智枢知识库入库失败：{exc}"
    else:
        document.status = "indexed"
        document.error_message = ""

    if vector_error:
        prefix = f"文本索引可用；向量索引失败：{vector_error}"
        document.error_message = f"{prefix}；{document.error_message}" if document.error_message else prefix

    document.indexed_at = db.func.now()
    db.session.commit()
    return document


def ensure_material_document(attachment):
    document = attachment.material_document
    if document:
        return document
    document = MaterialDocument(
        attachment_id=attachment.id,
        topic_id=attachment.topic_id,
        meeting_id=attachment.topic.meeting_id,
    )
    db.session.add(document)
    db.session.flush()
    return document


def known_zhishu_metadata_sync_error(exc):
    message = str(exc)
    return "metadatas" in message and "MetadataValue" in message


def index_chunk_vectors(chunks):
    if not chunks or not current_app.config.get("MATERIAL_RAG_VECTOR_ENABLED", True):
        return ""
    client = material_embedding_client()
    store = material_vector_store()
    if not client or not store:
        return ""
    try:
        vectors = []
        batch_size = max(1, int(current_app.config.get("MATERIAL_RAG_EMBEDDING_BATCH_SIZE", 16)))
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            vectors.extend(client.embed_texts([chunk.text for chunk in batch]))
        validate_vectors(vectors, len(chunks))
        store.upsert_chunks(chunks, vectors)
    except Exception as exc:
        message = str(exc)
        for chunk in chunks:
            chunk.embedding_status = "failed"
            chunk.embedding_error = message
        current_app.logger.warning("Material local vector index failed: %s", exc)
        return message

    model = getattr(client, "model", "") or current_app.config.get("MATERIAL_RAG_EMBEDDING_MODEL", "")
    for chunk, vector in zip(chunks, vectors):
        chunk.embedding_status = "indexed"
        chunk.embedding_model = model
        chunk.embedding_dim = len(vector)
        chunk.embedding_error = ""
    return ""


def validate_vectors(vectors, expected_count):
    if len(vectors) != expected_count:
        raise RuntimeError("embedding 返回数量与 chunk 数量不一致")
    dimensions = {len(vector) for vector in vectors if isinstance(vector, list)}
    if len(dimensions) != 1:
        raise RuntimeError("embedding 向量维度不一致")
    if len(dimensions) == 0:
        raise RuntimeError("embedding 未返回有效向量")


def extract_decrypted_attachment(path, file_type):
    return extract_attachment_text(path, file_type)


def chunk_material(text, chunk_chars=1400, overlap=150):
    normalized = "\n".join(line.strip() for line in (text or "").splitlines() if line.strip())
    if not normalized:
        return []
    sections = split_source_sections(normalized)
    chunks = []
    current = ""
    current_label = ""
    for label, section_text in sections:
        if len(section_text) > chunk_chars:
            flush_chunk(chunks, current, current_label)
            current = ""
            current_label = ""
            for part in sliding_chunks(section_text, chunk_chars, overlap):
                chunks.append({"source_label": label, "text": part})
            continue
        if current and len(current) + len(section_text) + 1 > chunk_chars:
            flush_chunk(chunks, current, current_label)
            tail = current[-overlap:].strip() if overlap and len(current) > overlap else ""
            current = f"{tail}\n{section_text}".strip() if tail else section_text
            current_label = label
        else:
            current = f"{current}\n{section_text}".strip() if current else section_text
            current_label = current_label or label
    flush_chunk(chunks, current, current_label)
    return [
        {**chunk, "chunk_index": index}
        for index, chunk in enumerate(chunks, start=1)
        if chunk["text"].strip()
    ]


def split_source_sections(text):
    pattern = re.compile(r"(?m)^(Slide \d+|Page \d+|Sheet \d+|Chunk \d+):\s*")
    matches = list(pattern.finditer(text))
    if not matches:
        return [("Chunk 1", text)]
    sections = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        label = match.group(1)
        sections.append((label, text[match.start():end].strip()))
    return sections


def sliding_chunks(text, chunk_chars, overlap):
    start = 0
    while start < len(text):
        end = min(len(text), start + chunk_chars)
        yield text[start:end].strip()
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)


def flush_chunk(chunks, text, label):
    if text and text.strip():
        chunks.append({"source_label": label or "Chunk 1", "text": text.strip()})


def sync_document_to_zhishu(document, path):
    client = zhishu_knowledge_client()
    if not client:
        return
    topic = document.topic
    meeting = document.meeting
    metadata = {
        "attachment_id": document.attachment_id,
        "topic_id": document.topic_id,
        "meeting_id": document.meeting_id,
        "filename": document.attachment.original_filename if document.attachment else "",
    }
    document.zhishu_file_id = client.upload_file(path, metadata=metadata)
    processed = client.wait_for_file_processed(
        document.zhishu_file_id,
        current_app.config.get("MATERIAL_RAG_INDEX_TIMEOUT_SECONDS", 60),
    )
    if not processed:
        raise TimeoutError("智枢文件处理超时")
    topic_kb_id = client.ensure_knowledge_base("topic", topic.id, f"议题材料：{topic.title}")
    document.zhishu_topic_knowledge_id = topic_kb_id
    client.add_file_to_knowledge(topic_kb_id, document.zhishu_file_id)
    if meeting:
        meeting_kb_id = client.ensure_knowledge_base("meeting", meeting.id, f"会议材料：{meeting.meeting_no}")
        document.zhishu_meeting_knowledge_id = meeting_kb_id
        client.add_file_to_knowledge(meeting_kb_id, document.zhishu_file_id)


def retrieve_topic_material_chunks(topic, query, source="ai_review", top_k=None):
    return retrieve_material_chunks(
        scope_type="topic",
        scope_id=topic.id,
        query=query,
        source=source,
        topic_id=topic.id,
        meeting_id=topic.meeting_id,
        top_k=top_k,
    )


def retrieve_meeting_material_chunks(meeting, query, source="copilot", visible_topic_ids=None, top_k=None):
    filters = [MaterialChunk.meeting_id == meeting.id]
    if visible_topic_ids is not None:
        filters.append(MaterialChunk.topic_id.in_(visible_topic_ids or [-1]))
    return retrieve_material_chunks(
        scope_type="meeting",
        scope_id=meeting.id,
        query=query,
        source=source,
        meeting_id=meeting.id,
        top_k=top_k,
        filters=filters,
        visible_topic_ids=visible_topic_ids,
    )


def retrieve_material_chunks(
    scope_type,
    scope_id,
    query,
    source,
    topic_id=None,
    meeting_id=None,
    top_k=None,
    filters=None,
    visible_topic_ids=None,
):
    top_k = int(top_k or current_app.config.get("MATERIAL_RAG_TOP_K", 12))
    base_filters = filters or []
    if not base_filters:
        if scope_type == "topic":
            base_filters.append(MaterialChunk.topic_id == scope_id)
        else:
            base_filters.append(MaterialChunk.meeting_id == scope_id)
    base_filters.append(MaterialDocument.status == "indexed")
    chunks = (
        MaterialChunk.query.join(MaterialDocument, MaterialChunk.document_id == MaterialDocument.id)
        .filter(*base_filters)
        .all()
    )
    selected, retrieval_mode, score_rows = retrieve_ranked_chunks(
        chunks,
        query,
        top_k,
        scope_type,
        scope_id,
        visible_topic_ids=visible_topic_ids,
    )
    log = MaterialRetrievalLog(
        source=source,
        topic_id=topic_id,
        meeting_id=meeting_id,
        query_text=query,
        scope_type=scope_type,
        scope_id=scope_id,
        chunk_ids=[chunk.id for chunk, _score, _meta in selected],
        retrieval_mode=retrieval_mode,
        scores_json=score_rows,
    )
    db.session.add(log)
    db.session.flush()
    db.session.commit()
    return [citation_with_scores(chunk, score, meta, retrieval_mode) for chunk, score, meta in selected]


def retrieve_ranked_chunks(chunks, query, top_k, scope_type, scope_id, visible_topic_ids=None):
    vector_ready = any(chunk.embedding_status == "indexed" for chunk in chunks)
    if current_app.config.get("MATERIAL_RAG_VECTOR_ENABLED", True) and vector_ready:
        try:
            vector_results = retrieve_vector_ranked_chunks(
                chunks,
                query,
                top_k,
                scope_type,
                scope_id,
                visible_topic_ids=visible_topic_ids,
            )
            if vector_results:
                return vector_results, "vector", score_rows_for_results(vector_results, "vector")
        except Exception as exc:
            current_app.logger.warning("Material vector retrieval failed, falling back to keyword: %s", exc)
            scored = score_chunks(chunks, query)
            selected = [(chunk, score, {"keyword_score": score}) for chunk, score in scored[:top_k]]
            return selected, "keyword_fallback", score_rows_for_results(selected, "keyword_fallback")
    scored = score_chunks(chunks, query)
    selected = [(chunk, score, {"keyword_score": score}) for chunk, score in scored[:top_k]]
    return selected, "keyword", score_rows_for_results(selected, "keyword")


def retrieve_vector_ranked_chunks(chunks, query, top_k, scope_type, scope_id, visible_topic_ids=None):
    client = material_embedding_client()
    store = material_vector_store()
    if not client or not store:
        return []
    query_vector = client.embed_texts([query or ""])
    validate_vectors(query_vector, 1)
    vector_hits = store.search(
        query_vector[0],
        top_k=max(top_k * 3, top_k),
        scope_type=scope_type,
        scope_id=scope_id,
        visible_topic_ids=visible_topic_ids,
    )
    chunk_by_id = {chunk.id: chunk for chunk in chunks}
    keyword_by_id = {chunk.id: score for chunk, score in score_chunks(chunks, query)}
    selected = []
    for hit in vector_hits:
        chunk = chunk_by_id.get(int(hit.get("chunk_id") or 0))
        if not chunk:
            continue
        vector_score = float(hit.get("vector_score") or 0)
        keyword_score = keyword_by_id.get(chunk.id, 0)
        final_score = combined_score(vector_score, keyword_score)
        selected.append(
            (
                chunk,
                final_score,
                {
                    "vector_score": vector_score,
                    "keyword_score": keyword_score,
                    "final_score": final_score,
                },
            )
        )
    return sorted(selected, key=lambda item: (item[1], item[0].id), reverse=True)[:top_k]


def combined_score(vector_score, keyword_score):
    vector_weight = float(current_app.config.get("MATERIAL_RAG_VECTOR_WEIGHT", 0.75))
    keyword_weight = float(current_app.config.get("MATERIAL_RAG_KEYWORD_WEIGHT", 0.25))
    normalized_keyword = 1 - math.exp(-max(0, float(keyword_score)))
    return vector_score * vector_weight + normalized_keyword * keyword_weight


def citation_with_scores(chunk, score, meta, retrieval_mode):
    data = chunk.citation_dict(score=score)
    data["retrieval_mode"] = retrieval_mode
    for key, value in (meta or {}).items():
        data[key] = value
    return data


def score_rows_for_results(results, retrieval_mode):
    rows = []
    for chunk, score, meta in results:
        rows.append(
            {
                "chunk_id": chunk.id,
                "score": score,
                "retrieval_mode": retrieval_mode,
                **(meta or {}),
            }
        )
    return rows


def score_chunks(chunks, query):
    tokens = query_tokens(query)
    scored = []
    for chunk in chunks:
        text = chunk.text or ""
        score = sum(text.lower().count(token) for token in tokens)
        scored.append((chunk, score))
    return sorted(scored, key=lambda item: (item[1], item[0].id), reverse=True)


def query_tokens(query):
    lowered = (query or "").lower()
    ascii_tokens = re.findall(r"[a-z0-9_]{2,}", lowered)
    cjk_tokens = re.findall(r"[\u4e00-\u9fff]{2,}", lowered)
    compact_cjk = "".join(cjk_tokens)
    window_tokens = [compact_cjk[index : index + 2] for index in range(max(0, len(compact_cjk) - 1))]
    return list(dict.fromkeys(ascii_tokens + cjk_tokens + window_tokens))


def indexed_attachment_context(attachment):
    document = attachment.material_document
    if not document:
        data = attachment_text_context(attachment)
        data["index_status"] = "not_indexed"
        data["warning"] = "该附件未进入材料知识库。"
        return data
    chunks = [
        chunk.citation_dict()
        for chunk in document.chunks.order_by(MaterialChunk.chunk_index.asc()).all()
    ]
    return {
        "attachment_id": attachment.id,
        "filename": attachment.original_filename,
        "file_type": attachment.effective_file_type,
        "file_size": attachment.file_size,
        "topic_id": attachment.topic_id,
        "topic_title": attachment.topic.title,
        "meeting_no": attachment.topic.meeting.meeting_no if attachment.topic.meeting else "",
        "index_status": document.status,
        "index_status_label": document.status_label,
        "warning": document.error_message or "",
        "chunks": chunks,
    }


def mark_attachment_material_deleted(attachment):
    document = attachment.material_document
    if not document:
        return
    document.status = "deleted"
    document.error_message = ""
    delete_document_vectors(document)
    MaterialChunk.query.filter_by(document_id=document.id).delete()
    db.session.flush()


def delete_document_vectors(document):
    try:
        store = material_vector_store()
        if store:
            store.delete_document(document.id)
    except Exception as exc:
        current_app.logger.warning("Material vector deletion failed: %s", exc)


def sha256_text(value):
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()
