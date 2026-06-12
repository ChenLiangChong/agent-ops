"""各 workflow 的階段邏輯（離線確定性版）＋ spec 評分器 ＋ 審批後 commit。

三個 registry：
- HANDLERS[action]   階段執行（DeterministicBrain 查這裡；ClaudeCliBrain 走 LLM 不用）
- GRADERS[kind]      spec 評分（QA 的「過不了不出貨」）
- COMMITS[approval]  審批消費後才執行的不可逆動作

加一條公司流程 = workflows/*.json ＋ 在這裡補 handler（或直接交給 LLM brain）。
handler 是 mock 但**資料形狀照官方**：USD Search 回應欄位、SimReady Foundation 規則、
Replicator writer 旗標都跟查證過的 API 一致——換真後端時只動 I/O，不動形狀。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

# ---------------------------------------------------------------- stage 執行環境


@dataclass
class StageCtx:
    db: sqlite3.Connection
    task: sqlite3.Row
    stage: object                 # workflow.Stage
    payload: dict
    injection: object             # memory.Injection（text + fact_ids）
    prior: dict                   # 前面 stage 的 output，key=stage_id
    run_id: int
    revision: int = 0             # 第幾次修訂（評分不過會帶 gaps 重跑）
    gaps: list = field(default_factory=list)


@dataclass
class StageResult:
    output: dict
    artifact_content: str
    artifact_ext: str = "json"
    approval_params: dict | None = None
    tokens: tuple = (0, 0)
    tool_calls: list = field(default_factory=list)   # [(tool_name, success)]
    notes: str = ""


def _toks(*objs) -> tuple:
    n = sum(len(json.dumps(o, ensure_ascii=False, default=str)) for o in objs)
    return (n // 3, n // 6)       # 模擬 token 量（離線 demo 用；真 brain 回報實際 usage）


# ---------------------------------------------------------------- simready-l1

_CLASS_ALIASES = {
    "FORKLIFT": "forklift", "FORK_LIFT": "forklift", "堆高機": "forklift",
    "RACK": "pallet_rack", "PALLET_RACK": "pallet_rack", "料架": "pallet_rack",
    "CONVEYOR": "conveyor", "輸送帶": "conveyor",
    "PALLET": "pallet", "棧板": "pallet",
    "AGV": "agv",
}
_DEFAULT_RACK_HEIGHT = 6.0        # CAD 圖層預設值（不可靠——這正是記憶飛輪 demo 的鉤子）


def etl_normalize(ctx: StageCtx) -> StageResult:
    """S0：DWG/DXF blocks ＋ WMS 匯出 → 正規化 IR（scene_objects）。"""
    blocks = ctx.payload["cad_blocks"]
    wms = ctx.payload.get("wms_rows", [])

    # 記憶驅動的行為差異：被教過「rack 高度以 WMS rack_height 為準」就改抓 WMS
    use_wms_height = any(
        "rack" in f.lower() and "wms" in f.lower()
        for f in _injected_facts(ctx)
    )
    wms_heights = [r["rack_height"] for r in wms if r.get("rack_height")]
    rack_height = max(wms_heights) if (use_wms_height and wms_heights) else _DEFAULT_RACK_HEIGHT

    with ctx.db:
        job = ctx.db.execute(
            "INSERT INTO ingest_jobs (task_id, status) VALUES (?, 'running')",
            (ctx.task["id"],),
        ).lastrowid
        objects = []
        for b in blocks:
            cls = _CLASS_ALIASES.get(b["block_name"].upper(), b["block_name"].lower())
            h = rack_height if cls == "pallet_rack" else b.get("height", 2.0)
            ctx.db.execute(
                """INSERT INTO scene_objects
                   (ingest_job_id, canonical_class, vendor_model, pose_x, pose_y, pose_theta,
                    footprint_w, footprint_d, height, attrs)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (job, cls, b.get("vendor_model"), b["x"], b["y"], b.get("theta", 0),
                 b.get("w", 1.0), b.get("d", 1.0), h, json.dumps(b.get("attrs", {}))),
            )
            objects.append({"class": cls, "height": h})
        ctx.db.execute("UPDATE ingest_jobs SET status='ok' WHERE id=?", (job,))

    ir = {
        "ingest_job_id": job,
        "objects": objects,
        "classes": sorted({o["class"] for o in objects}),
        "rack_height": rack_height,
        "rack_height_source": "wms.rack_height" if use_wms_height and wms_heights else "cad_layer_default",
        "wms_rows": len(wms),
    }
    return StageResult(
        output=ir,
        artifact_content=json.dumps(ir, ensure_ascii=False, indent=2),
        tokens=_toks(blocks, wms),
        tool_calls=[("parse_dxf_blocks", True), ("parse_wms_export", True)],
        notes=f"rack 高度來源：{ir['rack_height_source']}",
    )


# mock USD Search 目錄——回應欄位形狀照官方（url/score/bbox_dimension_x|y|z）
_USD_CATALOG = {
    "forklift":    {"url": "omniverse://library/vehicles/forklift_a.usd", "score": 0.93,
                    "bbox": (1.2, 2.4, 2.1)},
    "pallet_rack": {"url": "omniverse://library/storage/rack_4t.usd", "score": 0.88,
                    "bbox": (2.7, 1.1, 6.0)},
    "conveyor":    {"url": "omniverse://library/transport/belt_m.usd", "score": 0.85,
                    "bbox": (6.0, 0.8, 1.2)},
    "pallet":      {"url": "omniverse://library/storage/euro_pallet.usd", "score": 0.97,
                    "bbox": (1.2, 0.8, 0.15)},
}


def asset_match(ctx: StageCtx) -> StageResult:
    """S1：IR 類別 → USD Search 比對；未命中 → 缺件生成佇列（source='generated'）。"""
    classes = ctx.prior["etl"]["classes"]
    matched, generated, calls = [], [], []
    with ctx.db:
        for cls in classes:
            hit = _USD_CATALOG.get(cls)
            calls.append((f"usd_search:{cls}", True))
            if hit:
                aid = ctx.db.execute(
                    """INSERT INTO assets (usd_url, source, semantic_class, bbox_x, bbox_y, bbox_z,
                                           has_collider, has_mass, has_physical_material, has_semantics)
                       VALUES (?,?,?,?,?,?,1,1,1,1)""",
                    (hit["url"], "library", cls, *hit["bbox"]),
                ).lastrowid
                matched.append({"class": cls, "asset_id": aid, "score": hit["score"], "method": "text"})
            else:
                # 缺件：先生殼（物理屬性殘缺——SimReady 驗證階段會抓到並修）
                aid = ctx.db.execute(
                    """INSERT INTO assets (usd_url, source, semantic_class,
                                           has_collider, has_mass, has_physical_material, has_semantics,
                                           provenance)
                       VALUES (?,?,?,1,0,0,1,'metgen-internal-pipeline')""",
                    (f"omniverse://generated/{cls}.usd", "generated", cls),
                ).lastrowid
                generated.append({"class": cls, "asset_id": aid})
        obj_rows = ctx.db.execute(
            "SELECT id, canonical_class FROM scene_objects WHERE ingest_job_id=?",
            (ctx.prior["etl"]["ingest_job_id"],),
        ).fetchall()
        amap = {m["class"]: m for m in matched} | {g["class"]: {**g, "score": 0.75, "method": "text"}
                                                   for g in generated}
        for o in obj_rows:
            m = amap[o["canonical_class"]]
            ctx.db.execute(
                "INSERT OR IGNORE INTO asset_matches (scene_object_id, asset_id, score, method, fit_check) "
                "VALUES (?,?,?,?, 'pass')",
                (o["id"], m["asset_id"], m["score"], m["method"]),
            )
    out = {"matched": matched, "generated": generated,
           "avg_score": round(sum(m["score"] for m in matched) / max(len(matched), 1), 3)}
    return StageResult(
        output=out,
        artifact_content=json.dumps(out, ensure_ascii=False, indent=2),
        tokens=_toks(classes, out), tool_calls=calls,
        notes=f"庫內命中 {len(matched)}、缺件生成 {len(generated)}",
    )


_SIMREADY_RULES = ["PHYS-COLLIDER", "PHYS-MASS", "PHYS-MATERIAL", "SEM-LABEL"]
_RULE_FLAGS = {"PHYS-COLLIDER": "has_collider", "PHYS-MASS": "has_mass",
               "PHYS-MATERIAL": "has_physical_material", "SEM-LABEL": "has_semantics"}


def simready_validate(ctx: StageCtx) -> StageResult:
    """S2：對 SimReady Foundation 規則逐條驗。修訂輪（revision>0）補齊缺的物理屬性。"""
    with ctx.db:
        if ctx.revision > 0:
            # iterate→grade→revise：上一輪評分抓到的缺口，這輪補（mock＝補旗標；真版＝下 UsdPhysics API）
            for gap in ctx.gaps:
                aid, rule = gap["asset_id"], gap["rule_id"]
                ctx.db.execute(f"UPDATE assets SET {_RULE_FLAGS[rule]}=1 WHERE id=?", (aid,))
        ctx.db.execute("DELETE FROM asset_validations")  # 重驗（冪等）
        rows = ctx.db.execute("SELECT * FROM assets").fetchall()
        results, calls = [], []
        for a in rows:
            for rule in _SIMREADY_RULES:
                ok = bool(a[_RULE_FLAGS[rule]])
                ctx.db.execute(
                    "INSERT INTO asset_validations (asset_id, rule_id, status, detail) VALUES (?,?,?,?)",
                    (a["id"], rule, "pass" if ok else "fail",
                     None if ok else f"{a['semantic_class']} 缺 {rule}"),
                )
                results.append({"asset_id": a["id"], "class": a["semantic_class"],
                                "rule_id": rule, "status": "pass" if ok else "fail"})
                calls.append((f"validate:{rule}", ok))
    fails = [r for r in results if r["status"] == "fail"]
    out = {"total": len(results), "fails": fails, "revision": ctx.revision}
    return StageResult(
        output=out,
        artifact_content=json.dumps(out, ensure_ascii=False, indent=2),
        tokens=_toks(results), tool_calls=calls,
        notes=f"驗證 {len(results)} 條，fail {len(fails)}（第 {ctx.revision} 輪）",
    )


def scene_assemble(ctx: StageCtx) -> StageResult:
    """S3：組 USD stage ＋ 綁控制接點（PLC/ROS/WMS）。"""
    site = ctx.payload.get("site", "warehouse-A")
    with ctx.db:
        scene_id = ctx.db.execute(
            "INSERT INTO scenes (task_id, usd_stage_uri, site, layout_version, status) "
            "VALUES (?,?,?,?, 'validated')",
            (ctx.task["id"], f"omniverse://scenes/{site}.usd", site,
             ctx.payload.get("layout_version", "v1")),
        ).lastrowid
        for btype, ep in ctx.payload.get("bindings", {"wms": "wms://demo"}).items():
            ctx.db.execute(
                "INSERT OR IGNORE INTO scene_bindings (scene_id, binding_type, endpoint) VALUES (?,?,?)",
                (scene_id, btype, ep),
            )
    out = {"scene_id": scene_id, "site": site,
           "objects": len(ctx.prior["etl"]["objects"]),
           "bindings": ctx.payload.get("bindings", {"wms": "wms://demo"})}
    return StageResult(output=out, artifact_content=json.dumps(out, ensure_ascii=False, indent=2),
                       tokens=_toks(out), tool_calls=[("compose_usd_stage", True)])


def synthdata_config(ctx: StageCtx) -> StageResult:
    """S4：產 Replicator 設定（randomization ＋ writer 旗標照官方 BasicWriter）。"""
    cfg = {
        "randomizers": {
            "pose": {"jitter_xy_m": 0.15, "rot_z_deg": [0, 360]},
            "materials": {"pool": "industrial_pbr", "per_class": True},
            "lighting": {"intensity_lux": [200, 1200], "color_temp_k": [3500, 6500]},
            "camera": {"height_m": [3.0, 8.0], "pitch_deg": [-60, -20]},
        },
        "writer": {"name": "BasicWriter",
                   "annotators": {"rgb": True, "bounding_box_2d_tight": True,
                                  "semantic_segmentation": True, "distance_to_camera": True}},
    }
    with ctx.db:
        ds = ctx.db.execute(
            """INSERT INTO datasets (scene_id, randomization_config, writer, format, num_frames, seed, output_uri)
               VALUES (?,?,?,?,?,?,?)""",
            (ctx.prior["assemble"]["scene_id"], json.dumps(cfg["randomizers"]),
             "BasicWriter", "KITTI", ctx.payload.get("num_frames", 5000),
             ctx.payload.get("seed", 42), "s3://datasets/demo-batch/"),
        ).lastrowid
        for ann, on in cfg["writer"]["annotators"].items():
            if on:
                ctx.db.execute(
                    "INSERT OR IGNORE INTO dataset_artifacts (dataset_id, annotator, uri, count) VALUES (?,?,?,?)",
                    (ds, ann, f"s3://datasets/demo-batch/{ann}/", ctx.payload.get("num_frames", 5000)),
                )
    manifest = {"dataset_id": ds, "format": "KITTI",
                "num_frames": ctx.payload.get("num_frames", 5000), **cfg}
    return StageResult(output=manifest,
                       artifact_content=json.dumps(manifest, ensure_ascii=False, indent=2),
                       tokens=_toks(manifest), tool_calls=[("replicator_config", True)])


def publish_twin(ctx: StageCtx) -> StageResult:
    """S5：出貨前最後一站——只「準備」發佈並開審批單，不可逆動作在審批消費後的 COMMIT 執行。"""
    scene_id = ctx.prior["assemble"]["scene_id"]
    params = {"scene_id": scene_id, "site": ctx.prior["assemble"]["site"],
              "layout_version": ctx.payload.get("layout_version", "v1")}
    out = {"ready_to_publish": params,
           "dataset_id": ctx.prior["synthdata"]["dataset_id"]}
    return StageResult(output=out, artifact_content=json.dumps(out, ensure_ascii=False, indent=2),
                       approval_params=params, tokens=_toks(out))


def _commit_publish_twin(db: sqlite3.Connection, params: dict) -> str:
    with db:
        db.execute("UPDATE scenes SET status='published' WHERE id=?", (params["scene_id"],))
    return f"scene #{params['scene_id']} ({params['site']}) 已發佈"


# ---------------------------------------------------------------- code-change（通用工程流程）


def triage(ctx: StageCtx) -> StageResult:
    text = ctx.payload.get("issue_text", "")
    out = {"type": "bug" if any(k in text.lower() for k in ("bug", "錯", "壞", "skip")) else "feature",
           "area": ctx.payload.get("area", "general"),
           "plan": ["重現問題", "定位根因", "修補＋測試", "送 review"]}
    return StageResult(output=out, artifact_content=json.dumps(out, ensure_ascii=False, indent=2),
                       tokens=_toks(text, out))


def implement(ctx: StageCtx) -> StageResult:
    # 記憶驅動：被教過「PR 一定要附測試」→ patch 自帶測試段
    must_test = any("測試" in f for f in _injected_facts(ctx))
    diff = (
        "--- a/parser/csv_reader.py\n+++ b/parser/csv_reader.py\n"
        "@@ -10,7 +10,7 @@ def read_rows(path):\n"
        "-    for row in rows[1:]:   # BUG: 把第一筆資料當 header 跳掉\n"
        "+    for row in rows[has_header:]:\n"
    )
    if must_test:
        diff += ("--- /dev/null\n+++ b/tests/test_csv_reader.py\n"
                 "@@ -0,0 +1,4 @@\n+def test_first_row_kept():\n+    assert len(read_rows(FIXTURE)) == 3\n")
    out = {"patch_lines": diff.count("\n"), "has_tests": must_test,
           "has_description": True, "pr_ref": f"PR#{ctx.task['id']}", "sha": "abc1234"}
    return StageResult(output=out, artifact_content=diff, artifact_ext="diff",
                       tokens=_toks(diff), tool_calls=[("edit_file", True), ("run_tests", True)])


def code_review(ctx: StageCtx) -> StageResult:
    impl = ctx.prior["implement"]
    findings = []
    if not impl["has_tests"]:
        findings.append({"severity": "high", "msg": "缺測試"})
    if impl["patch_lines"] > 400:
        findings.append({"severity": "medium", "msg": "diff 過大，建議拆 PR"})
    out = {"findings": findings, "checked": ["has_tests", "diff_size", "has_description"], **impl}
    return StageResult(output=out, artifact_content=json.dumps(out, ensure_ascii=False, indent=2),
                       tokens=_toks(out))


def merge_pr(ctx: StageCtx) -> StageResult:
    impl = ctx.prior["implement"]
    params = {"pr_ref": impl["pr_ref"], "sha": impl["sha"]}
    return StageResult(output={"ready_to_merge": params},
                       artifact_content=json.dumps(params, ensure_ascii=False, indent=2),
                       approval_params=params, tokens=_toks(params))


def _commit_merge_pr(db: sqlite3.Connection, params: dict) -> str:
    return f"{params['pr_ref']} (sha {params['sha']}) 已 merge"


# ---------------------------------------------------------------- weekly-report（飛輪二號：人改 diff）


def collect_metrics(ctx: StageCtx) -> StageResult:
    from . import telemetry
    out = telemetry.summary(ctx.db)
    return StageResult(output=out, artifact_content=json.dumps(out, ensure_ascii=False, indent=2),
                       tokens=_toks(out))


def draft_report(ctx: StageCtx) -> StageResult:
    s = ctx.prior["collect"]
    md = (f"# 週報\n\n- 任務：{s['tasks_done']}/{s['tasks']} 完成\n"
          f"- agent 執行：{s['runs']} 次（訂閱 {s['subscription_runs']} 次）\n"
          f"- token：in {s['input_tokens']:,} / out {s['output_tokens']:,}\n"
          f"- API 成本：${s['cost_usd']}\n")
    return StageResult(output={"draft_chars": len(md)}, artifact_content=md, artifact_ext="md",
                       tokens=_toks(md))


# ---------------------------------------------------------------- registries


def _injected_facts(ctx: StageCtx) -> list[str]:
    if not ctx.injection or not ctx.injection.fact_ids:
        return []
    rows = ctx.db.execute(
        f"SELECT fact FROM memory_facts WHERE id IN ({','.join('?' * len(ctx.injection.fact_ids))})",
        ctx.injection.fact_ids,
    ).fetchall()
    return [r["fact"] for r in rows]


HANDLERS = {
    "etl_normalize": etl_normalize,
    "asset_match": asset_match,
    "simready_validate": simready_validate,
    "scene_assemble": scene_assemble,
    "synthdata_config": synthdata_config,
    "publish_twin": publish_twin,
    "triage": triage,
    "implement": implement,
    "code_review": code_review,
    "merge_pr": merge_pr,
    "collect_metrics": collect_metrics,
    "draft_report": draft_report,
}

COMMITS = {
    "publish_twin": _commit_publish_twin,
    "merge_pr": _commit_merge_pr,
}


# ---------------------------------------------------------------- spec 評分器（過不了不出貨）


def grade_simready(db: sqlite3.Connection, spec: dict, result: StageResult) -> dict:
    rows = db.execute("SELECT * FROM asset_validations WHERE rule_id IN ({})".format(
        ",".join("?" * len(spec["required_rules"]))), spec["required_rules"]).fetchall()
    fails = [r for r in rows if r["status"] == "fail"]
    gaps = [{"asset_id": r["asset_id"], "rule_id": r["rule_id"], "detail": r["detail"]} for r in fails]
    score = (len(rows) - len(fails)) / max(len(rows), 1)
    return {"score": round(score, 3), "passed": not fails, "gaps": gaps}


def grade_dataset(db: sqlite3.Connection, spec: dict, result: StageResult) -> dict:
    out = result.output
    gaps = []
    if out["format"] not in spec["allowed_formats"]:
        gaps.append({"rule_id": "FORMAT", "detail": out["format"]})
    if out["num_frames"] < spec["min_frames"]:
        gaps.append({"rule_id": "MIN-FRAMES", "detail": out["num_frames"]})
    have = set(out["writer"]["annotators"])
    for ann in spec["required_annotators"]:
        if ann not in have:
            gaps.append({"rule_id": "ANNOTATOR", "detail": f"缺 {ann}"})
    return {"score": 1.0 if not gaps else 0.5, "passed": not gaps, "gaps": gaps}


def grade_code_review(db: sqlite3.Connection, spec: dict, result: StageResult) -> dict:
    out = result.output
    gaps = []
    if spec.get("require_tests") and not out["has_tests"]:
        gaps.append({"rule_id": "TESTS", "detail": "缺測試"})
    if out["patch_lines"] > spec.get("max_diff_lines", 400):
        gaps.append({"rule_id": "DIFF-SIZE", "detail": out["patch_lines"]})
    high = [f for f in out["findings"] if f["severity"] == "high"]
    if high:
        gaps.append({"rule_id": "HIGH-FINDINGS", "detail": [f["msg"] for f in high]})
    return {"score": 1.0 - 0.3 * len(gaps), "passed": not gaps, "gaps": gaps}


GRADERS = {
    "simready": grade_simready,
    "dataset": grade_dataset,
    "code-review": grade_code_review,
}
