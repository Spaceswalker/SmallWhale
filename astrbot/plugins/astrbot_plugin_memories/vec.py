
# 出处：astrbot_plugin_memories（原作者：若剑清，MIT License）
# 本项目在原作者 v0.5.1 的基础上做了修改（记忆注入、日记总结、语言过滤等，详见 THIRD-PARTY.md）。
# 修改者：XiaoJing contributors

"""vec.py —— 本地语义检索（bge-small-zh-v1.5 量化 ONNX + onnxruntime CPU）。

- 装不上依赖 / 模型文件没下载时，`enabled()` 返回 False，调用方自动回落纯 BM25。
- 索引存 `vec_index.npy`（n×512 float32）+ `vec_ids.json`（id 列表），增量更新。
- 全本地，聊天内容不出这台机器（聊天记录不出门的语义检索）。
"""

import json
import os
import threading
import time

import numpy as np

_DATA_DIR = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "..",
        "plugin_data",
        "astrbot_plugin_memories",
    )
)
_MODEL_DIR = os.path.join(_DATA_DIR, "bge-small-zh-v1.5")
_MODEL_ONNX = os.path.join(_MODEL_DIR, "model_quantized.onnx")
_TOK_JSON = os.path.join(_MODEL_DIR, "tokenizer.json")
_IDS_PATH = os.path.join(_DATA_DIR, "vec_ids.json")
_IDX_PATH = os.path.join(_DATA_DIR, "vec_index.npy")

_DIM = 512
_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："

_lock = threading.Lock()
_sess = None
_tok = None
_matrix = None  # n×512 float32
_ids: list = []
_dirty = False
_flushed_len = 0  # 上次落盘时的行数
_last_flush = 0.0


def enabled() -> bool:
    try:
        import onnxruntime  # noqa: F401
    except Exception:
        return False
    return os.path.isfile(_MODEL_ONNX) and os.path.isfile(_TOK_JSON)


def _load() -> bool:
    global _sess, _tok
    if _sess is not None:
        return True
    try:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        so = ort.SessionOptions()
        so.intra_op_num_threads = 4
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        _sess = ort.InferenceSession(
            _MODEL_ONNX, sess_options=so, providers=["CPUExecutionProvider"]
        )
        _tok = Tokenizer.from_file(_TOK_JSON)
        return True
    except Exception:
        _sess = None
        _tok = None
        return False


def embed(texts) -> "np.ndarray | None":
    if not _load():
        return None
    out = np.zeros((len(texts), _DIM), dtype=np.float32)
    pad_id = 0
    try:
        _p = _tok.token_to_id("[PAD]")
        pad_id = _p if _p is not None else 0
    except Exception:
        pass
    for i, t in enumerate(texts):
        enc = _tok.encode(t if t else " ")
        ids = enc.ids[:_DIM]
        mask = [1] * len(ids)
        while len(ids) < 16:
            ids.append(pad_id)
            mask.append(0)
        tt = [0] * len(ids)
        feed = {
            "input_ids": np.asarray([ids], dtype=np.int64),
            "attention_mask": np.asarray([mask], dtype=np.int64),
            "token_type_ids": np.asarray([tt], dtype=np.int64),
        }
        res = _sess.run(None, feed)
        hs = np.asarray(res[0][0], dtype=np.float32)
        if hs.ndim == 2 and hs.shape[1] == _DIM:
            am = np.asarray(mask, dtype=np.float32)[:, None]
            v = (hs * am).sum(axis=0) / max(1e-9, float(am.sum()))
        else:
            v = hs.flatten()[: _DIM]
        n = float(np.linalg.norm(v))
        if n > 1e-9:
            v = v / n
        out[i] = v
    return out


def query(text: str, k: int = 8, min_sim: float = 0.55):
    if not enabled() or _matrix is None or len(_ids) == 0:
        return []
    v = embed([(_QUERY_PREFIX + (str(text or "")).strip())[:400]])
    if v is None:
        return []
    sims = _matrix @ v[0]
    order = np.argsort(-sims)[: min(k, len(_ids))]
    out = []
    for i in order:
        s = float(sims[i])
        if s < min_sim:
            continue
        out.append((str(_ids[i]), s))
    return out


def _memo_text(row) -> str:
    b = str(row.get("body") or "").strip()
    if b[:2] in ("我：", "她：", "他："):
        b = b[2:].strip()
    return " ".join(b.split())[:180]


def _flush_locked() -> bool:
    """把内存矩阵与 id 列表落盘。返回是否成功。"""
    global _dirty, _flushed_len, _last_flush
    try:
        np.save(_IDX_PATH, _matrix)
        with open(_IDS_PATH, "w", encoding="utf-8") as f:
            json.dump(_ids, f)
        _dirty = False
        _flushed_len = len(_ids)
        _last_flush = time.time()
        return True
    except Exception:
        return False


def add_many(rows_iter) -> int:
    """PET3:vecinc —— 只把新行的向量追加进内存矩阵，不写盘；真正落盘由 maybe_flush 节流。"""
    global _matrix, _ids, _dirty, _flushed_len, _last_flush
    if not enabled() or not _load():
        return 0
    rows = list(rows_iter)
    if not rows:
        return 0
    with _lock:
        if _matrix is None:
            if os.path.isfile(_IDX_PATH):
                try:
                    _matrix = np.load(_IDX_PATH)
                except Exception:
                    _matrix = None
            if _matrix is None or _matrix.ndim != 2:
                _matrix = np.zeros((0, _DIM), dtype=np.float32)
                _ids = []
            elif os.path.isfile(_IDS_PATH):
                try:
                    with open(_IDS_PATH, "r", encoding="utf-8") as f:
                        stored = json.load(f)
                    if len(stored) == _matrix.shape[0]:
                        _ids = list(stored)
                        _flushed_len = len(_ids)
                        _last_flush = time.time()
                except Exception:
                    pass
        existing = set(_ids)
        need = [r for r in rows if str(r.get("id")) not in existing]
        if not need:
            return 0
        texts = [_memo_text(r) for r in need]
        try:
            batch = embed(texts)
        except Exception:
            batch = None
        if batch is None:
            return 0
        _matrix = (
            np.concatenate([_matrix, batch], axis=0) if _matrix.shape[0] else batch
        )
        _ids.extend(str(r.get("id")) for r in need)
        _dirty = True
        return len(need)


def maybe_flush(force: bool = False) -> bool:
    """PET3:vecflush —— 增量达到 50 行或距上次落盘 ≥300 秒才写整块 npy，否则攒着。

    force=True（维护/关停）时无视节流直接写。"""
    global _dirty
    if not _dirty:
        return True
    with _lock:
        if not _dirty:
            return True
        if not force:
            now = time.time()
            if (now - _last_flush < 300.0) and (len(_ids) - _flushed_len < 50):
                return False
        return _flush_locked()


def ensure_index(rows_iter) -> tuple[int, int]:
    """增量构建向量索引。rows_iter 一次性迭代器。返回 (已索引数, 库总数)。"""
    global _matrix, _ids, _dirty, _flushed_len, _last_flush
    if not enabled() or not _load():
        return 0, 0
    rows = list(rows_iter)
    with _lock:
        stored_ids = []
        try:
            with open(_IDS_PATH, "r", encoding="utf-8") as f:
                stored_ids = json.load(f)
        except Exception:
            stored_ids = []
        cur_ids = {str(r.get("id")) for r in rows}
        if _matrix is None:
            if os.path.isfile(_IDX_PATH):
                try:
                    _matrix = np.load(_IDX_PATH)
                except Exception:
                    _matrix = None
            if _matrix is not None and _matrix.ndim == 2 and len(stored_ids) == _matrix.shape[0]:
                _ids = list(stored_ids)
                _flushed_len = len(_ids)
                _last_flush = time.time()
        if _matrix is None:
            _matrix = np.zeros((0, _DIM), dtype=np.float32)
            _ids = []
        # 删掉已不在库里的行
        keep = [i for i, mid in enumerate(_ids) if mid in cur_ids]
        _ids = [_ids[i] for i in keep]
        if keep:
            _matrix = _matrix[keep]
        else:
            _matrix = np.zeros((0, _DIM), dtype=np.float32)
        existing = set(_ids)
        need = [r for r in rows if str(r.get("id")) not in existing]
        if need:
            texts = [_memo_text(r) for r in need]
            try:
                batch = embed(texts)
            except Exception:
                batch = None
            if batch is not None:
                _matrix = (
                    np.concatenate([_matrix, batch], axis=0)
                    if _matrix.shape[0]
                    else batch
                )
                _ids.extend(str(r.get("id")) for r in need)
                _dirty = True
        if _dirty:
            _flush_locked()
        return len(_ids), len(rows)
