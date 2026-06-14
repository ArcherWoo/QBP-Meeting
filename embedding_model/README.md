# Embedding Model Directory

本目录用于放置本地离线 embedding 模型。模型文件体积通常较大，不直接提交到 Git。

默认模型路径：

```text
embedding_model/sentence-transformers/all-MiniLM-L6-v2/
```

应用配置默认读取 `MATERIAL_RAG_LOCAL_EMBEDDING_MODEL_PATH`，未配置时会使用上面的路径。

放置模型时，请保持 SentenceTransformer 目录结构完整，例如：

```text
embedding_model/
  sentence-transformers/
    all-MiniLM-L6-v2/
      config.json
      modules.json
      tokenizer.json
      ...
```

如果只使用智枢 embedding 接口，可以不放本地模型。
