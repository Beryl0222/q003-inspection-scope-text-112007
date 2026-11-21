"""督察抽查线索分析与下一步抽点计划。

读入抽检原始记录、运输拆并箱单、供应批次关系、分厂复核说明与
现场名额，输出：
 1. 现场描述拆解（部位 / 条件 / 结果 / 引用 / 否定 / 样号歧义）；
 2. 独立样品、重复复看与身份不明记录的归属；
 3. 同源（同供应批次）证据链、扩围触发判断与无效阴性；
 4. 可能流向与未检使用点；
 5. 按规程优先级排出的下一轮抽查计划与名额、签收处置。

只依赖标准库；建议以 ``python3 -I`` 运行，避免资料目录中的同名
模块被导入。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from inspect_records import load_records

# ---------------------------------------------------------------- 词典
# 部位线索 -> 规范部位
PART_CUES: dict[str, str] = {
    "转轴": "转轴",
    "根部": "转轴",
    "入座处": "转轴",
    "垫板": "垫板",
    "压盖": "压盖",
    "壳面": "壳面",
}
# 缺陷线索 -> (规范缺陷, 规范部位)；部位为空时由部位线索决定
DEFECT_CUES: dict[str, tuple[str, str]] = {
    "断纹": ("裂纹", "转轴"),
    "根裂": ("裂纹", "转轴"),
    "裂纹": ("裂纹", ""),
    "裂": ("裂纹", ""),
    "划伤": ("划伤", "壳面"),
    "松": ("松动", ""),
    "松动": ("松动", ""),
}
# 否定触发词（仅作用于所在分句）
NEG_CUES = ("未复现", "没复现", "没有复现", "未见", "没松", "未查看", "未检查")
# 维修 / 换件线索（出现在 conditions 或正文即视为维修后复看）
REPAIR_CUES = ("更换", "换轴", "维修", "换新")
# 对维修线索的否定（“未换轴”“未维修”等表示尚未维修，是原始状态复看）
REPAIR_NEGATIONS = ("未", "没", "无", "尚未")
# 引用上一条线索的提示
REFERENCE_CUES = ("上一条", "上轮", "上一箱", "上轮的")
# 样号抄写歧义提示
AMBIGUOUS_CUES = ("也可能", "可能是", "抄成", "不确定", "存疑")

SPLIT_RE = re.compile(r"[，。；、,.!?！？\s]+")
FULL_SERIAL_RE = re.compile(r"L-\d{3}")
SERIAL_SUFFIX_RE = re.compile(r"(?<!\d)(\d{2,3})(?!\d)")
HANDOVER_RE = re.compile(r"H\d+")
NOTE_REF_RE = re.compile(r"N\d+")


def detect_repair(*texts: str) -> bool:
    """判断是否为维修/换件后的复看。

    命中维修线索，但其紧邻前字是否定词（如“未换轴”“未维修”）时，
    表示仍处于原始未修状态，不算维修后复看。
    """

    for text in texts:
        if not text:
            continue
        for cue in REPAIR_CUES:
            start = 0
            while True:
                idx = text.find(cue, start)
                if idx < 0:
                    break
                if idx == 0 or text[idx - 1] not in REPAIR_NEGATIONS:
                    return True
                start = idx + len(cue)
    return False


# ---------------------------------------------------------------- 数据结构
@dataclass
class Finding:
    """一条分句级观察：部位 + 缺陷 + 极性。"""

    part: str
    defect: str
    polarity: str  # positive / negative
    clause: str


@dataclass
class ParsedNote:
    """现场描述拆解结果。"""

    id: str
    handover: str
    conditions: str
    findings: list[Finding]
    serial_confirmed: list[str] = field(default_factory=list)
    serial_ambiguous: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    repair_after: bool = False
    raw_text: str = ""


@dataclass
class NoteAttribution:
    """一条笔录对样品的归属判断。"""

    note_id: str
    kind: str  # independent 独立样品首证 / duplicate 同一样品复看 /
    #            unidentified 身份不明 / corrected 经补录落实
    serial: str | None
    handover: str
    possible_serials: list[str]
    reasons: list[str]


@dataclass
class Sample:
    """可确认身份的独立样品（按序列号）。"""

    serial: str
    component_lot: str | None
    plant: str | None
    note_ids: list[str]
    handovers: list[str]
    positive: list[str] = field(default_factory=lambda: [])
    negative: list[str] = field(default_factory=lambda:[])
    sites: list[str] = field(default_factory=list)


@dataclass
class NegativeRecord:
    note_id: str
    part: str
    defect: str
    conditions: str
    repair_after: bool
    counts: bool
    reason: str


@dataclass
class FamilyStatus:
    """同一部位同类异常的证据族与扩围判断。"""

    part: str
    defect: str
    required_conditions: str | None
    independent_serials: list[str]
    evidence_notes: list[str]
    trigger_met: bool
    reasons: list[str]
    negatives: list[NegativeRecord]


@dataclass
class LotFlow:
    component_lot: str
    serials: list[str]
    confirmed_sites: list[str]
    possible_sites: list[str]
    uninspected_sites: list[str]
    inspected_sites: list[str]


@dataclass
class PlanAction:
    site_id: str
    site_name: str
    priority: int
    tier: str  # same_source 扩围 / contrast 对照 / none 无判别目标
    reason: str
    target_serials: list[str]
    required_conditions: str | None
    available_slots: int


@dataclass
class ReceiptDisposition:
    receipt_id: str
    site_id: str
    type: str
    item: str | None
    slot_used: bool
    decision: str  # executed / withdrawable
    reason: str


@dataclass
class CorrectionEffect:
    correction_id: str
    note_id: str
    confirmed_serial: str | None
    invalidated_handover: str | None
    summary: str


@dataclass
class Plan:
    request_id: str
    remaining_total: int
    hypotheses: list[str]
    triggers: list[FamilyStatus]
    assigned: list[PlanAction]
    leftover_slots: int
    leftover_reason: str
    candidates: list[PlanAction]
    receipts: list[ReceiptDisposition]
    covered_sites: list[str]


# ---------------------------------------------------------------- 描述拆解
def parse_note(row: dict[str, Any]) -> ParsedNote:
    """把一条现场描述拆成部位、条件、结果、引用、否定与样号信息。"""

    text = row.get("text", "") or ""
    conditions = row.get("conditions", "") or ""
    findings: list[Finding] = []
    repair_after = detect_repair(conditions, text)

    for clause in [c for c in SPLIT_RE.split(text) if c]:
        part = next((name for cue, name in PART_CUES.items() if cue in clause), "")
        matched = [
            (cue, spec) for cue, spec in DEFECT_CUES.items() if cue in clause
        ]
        if not matched:
            continue
        # 取最长缺陷词，避免“断纹”被单字“裂”抢先
        cue, (defect, defect_part) = max(matched, key=lambda item: len(item[0]))
        part = part or defect_part or "未注明部位"
        polarity = "negative" if any(neg in clause for neg in NEG_CUES) else "positive"
        findings.append(Finding(part=part, defect=defect, polarity=polarity, clause=clause))

    full_serials = FULL_SERIAL_RE.findall(text)
    ambiguous: list[str] = []
    confirmed: list[str] = list(full_serials)
    if not full_serials:
        # “最后两位抄成了 91，也可能是 97” -> 候选样号（结合箱单候选落实）
        suffixes = SERIAL_SUFFIX_RE.findall(text)
        if suffixes and any(cue in text for cue in AMBIGUOUS_CUES):
            ambiguous = [f"L-{s.zfill(3)}" for s in suffixes]
    elif any(cue in text for cue in AMBIGUOUS_CUES):
        ambiguous = list(full_serials)
        confirmed = []

    references = NOTE_REF_RE.findall(text)
    if any(cue in text for cue in REFERENCE_CUES) and not references:
        references = ["上一条"]

    return ParsedNote(
        id=row["id"],
        handover=row.get("handover", ""),
        conditions=conditions,
        findings=findings,
        serial_confirmed=confirmed,
        serial_ambiguous=ambiguous,
        references=references,
        repair_after=repair_after,
        raw_text=text,
    )


# ---------------------------------------------------------------- 补录解析
def parse_correction(row: dict[str, Any], logistics: list[dict[str, Any]]) -> CorrectionEffect:
    """从质检员补录中提取可核实来源：确认样号与失效交接联号。"""

    text = row.get("text", "") or ""
    serials = FULL_SERIAL_RE.findall(text)
    handovers = HANDOVER_RE.findall(text)
    valid_handover_ids = {h["id"] for h in logistics}
    invalidated = next(
        (h for h in handovers if h not in valid_handover_ids or "误贴" in text),
        None,
    )
    return CorrectionEffect(
        correction_id=row["id"],
        note_id=row.get("note", ""),
        confirmed_serial=serials[0] if serials else None,
        invalidated_handover=invalidated,
        summary=text,
    )


# ---------------------------------------------------------------- 归属
def attribute_notes(
    parsed: list[ParsedNote],
    logistics: list[dict[str, Any]],
    corrections: list[CorrectionEffect],
) -> tuple[list[NoteAttribution], dict[str, str], set[str]]:
    """决定每条笔录是独立样品、重复复看还是身份不明。

    返回 (归属列表, 笔录->确认样号, 被补录判失效的交接联号集合)。
    """

    corrected_serial: dict[str, str] = {
        c.note_id: c.confirmed_serial for c in corrections if c.confirmed_serial
    }
    invalid_handovers = {c.invalidated_handover for c in corrections if c.invalidated_handover}
    handover_candidates = {
        h["id"]: list(h.get("serial_candidates", [])) for h in logistics
    }

    attributions: list[NoteAttribution] = []
    note_serial: dict[str, str] = {}
    seen_serials: set[str] = set()

    for index, note in enumerate(parsed):
        possible = list(note.serial_confirmed or note.serial_ambiguous)
        reasons: list[str] = []
        serial: str | None = None

        if note.id in corrected_serial:
            serial = corrected_serial[note.id]
            reasons.append("质检员补录核实来源，样号按补录落实")
        elif note.serial_confirmed:
            serial = note.serial_confirmed[0]
            reasons.append("正文写明完整样号")
        elif note.references and index > 0:
            # 引用上一条且同一交接联：复看同一样品，继承上一条落实的样号
            previous = parsed[index - 1]
            if (
                previous.handover == note.handover
                and previous.id in note_serial
            ):
                serial = note_serial[previous.id]
                reasons.append(
                    f"引用 {previous.id} 且交接联相同，按同一样品的重复复看继承样号"
                )
        if serial is None and note.id not in corrected_serial and not note.serial_confirmed:
            candidates = [
                s for s in handover_candidates.get(note.handover, [])
                if note.handover not in invalid_handovers
            ]
            if note.serial_ambiguous:
                candidates = [s for s in candidates if s in note.serial_ambiguous] or candidates
                reasons.append("样号抄写存疑，仅保留候选归属")
            if len(candidates) == 1:
                serial = candidates[0]
                reasons.append(f"交接联 {note.handover} 仅含一个候选样号")
            elif len(candidates) > 1:
                possible = candidates
                reasons.append(f"交接联 {note.handover} 含多个候选样号，身份不能确认")

        if serial is not None:
            note_serial[note.id] = serial
            if serial in seen_serials:
                kind = "duplicate"
                reasons.append("该样号已有独立首证，本条按同一样品复看处理")
            else:
                kind = "independent" if note.id not in corrected_serial else "corrected"
                seen_serials.add(serial)
        else:
            kind = "unidentified"

        attributions.append(
            NoteAttribution(
                note_id=note.id,
                kind=kind,
                serial=serial,
                handover=note.handover,
                possible_serials=possible,
                reasons=reasons,
            )
        )
    return attributions, note_serial, invalid_handovers


def build_samples(
    parsed: list[ParsedNote],
    note_serial: dict[str, str],
    assembly: list[dict[str, Any]],
    logistics: list[dict[str, Any]],
    invalid_handovers: set[str],
) -> dict[str, Sample]:
    """按序列号合并样品；同一序列号的多条笔录只算一件。"""

    assembly_map = {a["id"]: a for a in assembly}
    handover_map = {h["id"]: h for h in logistics}
    samples: dict[str, Sample] = {}

    for note in parsed:
        serial = note_serial.get(note.id)
        if serial is None:
            continue
        info = assembly_map.get(serial, {})
        sample = samples.setdefault(
            serial,
            Sample(
                serial=serial,
                component_lot=info.get("component_lot"),
                plant=info.get("plant"),
                note_ids=[],
                handovers=[],
            ),
        )
        sample.note_ids.append(note.id)
        if note.handover and note.handover not in sample.handovers:
            sample.handovers.append(note.handover)
            handover = handover_map.get(note.handover)
            if handover and note.handover not in invalid_handovers:
                site = handover["to"]
                if site not in sample.sites:
                    sample.sites.append(site)
        for finding in note.findings:
            bucket = sample.positive if finding.polarity == "positive" else sample.negative
            label = f"{note.id}:{finding.part}{finding.defect}"
            if label not in bucket:
                bucket.append(label)
    return samples


# ---------------------------------------------------------------- 证据族与扩围
def evaluate_families(
    parsed: list[ParsedNote],
    note_serial: dict[str, str],
    samples: dict[str, Sample],
) -> list[FamilyStatus]:
    """按部位+缺陷聚族，核对扩围触发条件并登记阴性观察的效力。

    只有阳性观察建立证据族；纯否定（如“压盖没松”）挂到同部位同缺陷
    的阳性族，若没有对应阳性族则单列一条无证据族，绝不把阴性当阳性计数。
    身份不明（无确认样号）的笔录只登记，不计入独立样品与扩围证据。
    """

    by_note = {n.id: n for n in parsed}
    positive_notes: dict[tuple[str, str], list[str]] = {}
    negative_notes: dict[tuple[str, str], list[str]] = {}

    for note in parsed:
        for finding in note.findings:
            key = (finding.part, finding.defect)
            if finding.polarity == "positive":
                positive_notes.setdefault(key, []).append(note.id)
            else:
                negative_notes.setdefault(key, []).append(note.id)

    all_keys = sorted(set(positive_notes) | set(negative_notes))
    families: list[FamilyStatus] = []
    for part, defect in all_keys:
        # 身份不明的阳性笔录不进证据链，仅在原因中说明
        raw_positive = positive_notes.get((part, defect), [])
        p_notes = [nid for nid in raw_positive if nid in note_serial]
        unidentified_positive = [nid for nid in raw_positive if nid not in note_serial]
        n_notes = negative_notes.get((part, defect), [])
        serials: list[str] = []
        evidence: list[str] = []
        required_conditions: str | None = None
        for note_id in p_notes:
            evidence.append(note_id)
            serial = note_serial.get(note_id)
            if serial and serial not in serials:
                serials.append(serial)
            cond = by_note[note_id].conditions
            if required_conditions is None:
                required_conditions = cond

        # 同供应批次 + 同工况 + 同部位同类异常 的独立样品
        qualifying: list[str] = []
        for serial in serials:
            sample = samples.get(serial)
            if sample is None:
                continue
            same_cond = any(
                by_note[nid].conditions == required_conditions
                and any(
                    f.part == part and f.defect == defect and f.polarity == "positive"
                    for f in by_note[nid].findings
                )
                for nid in sample.note_ids
            )
            if same_cond:
                qualifying.append(serial)
        # 触发要求：至少两件可确认独立、同一供应批次、同工况同部位同类
        lot_counts: dict[str, list[str]] = {}
        for serial in qualifying:
            lot = samples[serial].component_lot
            if lot:
                lot_counts.setdefault(lot, []).append(serial)
        trigger_lot = next((lot for lot, members in lot_counts.items() if len(members) >= 2), None)
        trigger_met = trigger_lot is not None
        reasons: list[str] = []
        if trigger_met:
            members = lot_counts[trigger_lot]
            reasons.append(
                f"供应批次 {trigger_lot} 有 {len(members)} 件可确认独立样品"
                f"（{'、'.join(members)}）在「{required_conditions}」下观察到{part}{defect}"
            )
        elif p_notes:
            reasons.append(
                f"同部位同类异常仅 {len(serials)} 件独立样品，且不满足同批次两件门槛"
            )
        else:
            reasons.append("仅有否定观察、没有阳性线索，不构成证据族，不能计入扩围证据")
        if unidentified_positive:
            reasons.append(
                f"阳性笔录 {'、'.join(unidentified_positive)} 身份不明，"
                "不能确认独立样品，不计入扩围触发计数"
            )

        negatives: list[NegativeRecord] = []
        for note_id in n_notes:
            note = by_note[note_id]
            counts = True
            why: list[str] = []
            if note.repair_after:
                counts = False
                why.append("维修/换件后的复看，不能替代出货时观察")
            if required_conditions is not None and note.conditions != required_conditions:
                counts = False
                why.append(
                    f"观察工况「{note.conditions or '未注明'}」与阳性工况"
                    f"「{required_conditions or '未注明'}」不同，阴性不具撤控效力"
                )
            if counts:
                why.append(
                    "同工况阴性，可作为对照观察登记"
                    if required_conditions is not None
                    else "无对应阳性线索，阴性仅登记备查"
                )
            negatives.append(
                NegativeRecord(
                    note_id=note_id,
                    part=part,
                    defect=defect,
                    conditions=note.conditions,
                    repair_after=note.repair_after,
                    counts=counts,
                    reason="；".join(why),
                )
            )

        families.append(
            FamilyStatus(
                part=part,
                defect=defect,
                required_conditions=required_conditions,
                independent_serials=serials,
                evidence_notes=evidence,
                trigger_met=trigger_met,
                reasons=reasons,
                negatives=negatives,
            )
        )
    return families


# ---------------------------------------------------------------- 流向
def trace_lot_flow(
    samples: dict[str, Sample],
    assembly: list[dict[str, Any]],
    logistics: list[dict[str, Any]],
    invalid_handovers: set[str],
    inspected_sites: set[str],
    name_to_id: dict[str, str],
) -> list[LotFlow]:
    """按供应批次汇总确认去向与可能去向，并标出未检使用点。

    物流单用店名登记，统一换算成站点 id；未在 site 表中的名字原样保留。
    """

    flows: dict[str, LotFlow] = {}
    for row in assembly:
        lot = row["component_lot"]
        flow = flows.setdefault(
            lot,
            LotFlow(lot, [], [], [], [], []),
        )
        flow.serials.append(row["id"])

    for handover in logistics:
        if handover["id"] in invalid_handovers:
            continue
        site = name_to_id.get(handover["to"], handover["to"])
        for serial in handover.get("serial_candidates", []):
            lot = next((a["component_lot"] for a in assembly if a["id"] == serial), None)
            if lot is None:
                continue
            flow = flows[lot]
            if serial in set(samples):
                if site not in flow.confirmed_sites:
                    flow.confirmed_sites.append(site)
            if site not in flow.possible_sites:
                flow.possible_sites.append(site)

    for flow in flows.values():
        flow.inspected_sites = [s for s in flow.possible_sites if s in inspected_sites]
        flow.uninspected_sites = [s for s in flow.possible_sites if s not in inspected_sites]
    return [flows[k] for k in sorted(flows)]


# ---------------------------------------------------------------- 计划
def build_plan(
    records: dict[str, Any],
    families: list[FamilyStatus],
    flows: list[LotFlow],
    samples: dict[str, Sample],
    attributions: list[NoteAttribution],
    corrections: list[CorrectionEffect],
) -> Plan:
    """按规程生成下一轮抽点计划。"""

    request = records["request"][0]
    sites = {s["id"]: s for s in records["site"]}
    site_name = {s["id"]: s["name"] for s in records["site"]}
    remaining_total = int(request["remaining_total"])
    hypotheses = list(request.get("hypotheses", []))
    covered = list(request.get("covered_sites", []))
    invalid_handovers = {
        c.invalidated_handover for c in corrections if c.invalidated_handover
    }

    inspected_sites = set(covered)
    # 已在笔录中实际观察过的现场也算已检查（物流单按店名登记，需反查站点 id）；
    # 被补录判失效的交接联不构成对其去向现场的检查
    site_to_id = {s["name"]: s["id"] for s in records["site"]}
    handover_to_site = {
        h["id"]: site_to_id.get(h["to"], h["to"]) for h in records["logistics"]
    }
    for attr in attributions:
        if attr.kind == "unidentified" or attr.handover in invalid_handovers:
            continue
        inspected_sites.add(handover_to_site.get(attr.handover, ""))
    inspected_sites.discard("")

    triggered = [f for f in families if f.trigger_met]
    trigger_lots = {
        samples[s].component_lot
        for f in triggered
        for s in f.independent_serials
        if s in samples and samples[s].component_lot
    }

    # ---- 候选现场：先同源扩围，再对照
    actions: list[PlanAction] = []
    for site_id, site in sites.items():
        remaining = int(site["remaining"])
        if remaining <= 0:
            continue
        on_site = [
            h for h in records["logistics"]
            if h["to"] == site_name[site_id] and h["id"] not in
            {c.invalidated_handover for c in corrections if c.invalidated_handover}
        ]
        serials_here = sorted({s for h in on_site for s in h.get("serial_candidates", [])})
        assembly_map = {a["id"]: a for a in records["assembly"]}
        lots_here = {assembly_map[s]["component_lot"] for s in serials_here if s in assembly_map}
        plants_here = {assembly_map[s]["plant"] for s in serials_here if s in assembly_map}

        same_source = sorted(
            s for s in serials_here
            if assembly_map.get(s, {}).get("component_lot") in trigger_lots
        )
        # 对照价值对所有现场都计算：同厂异部件批次，或异厂同部件批次
        trigger_plants = {
            samples[s].plant
            for f in triggered for s in f.independent_serials if s in samples
        }
        contrast_notes: list[str] = []
        if plants_here & trigger_plants and lots_here - trigger_lots:
            contrast_notes.append("同厂异部件批次对照（区分本地装配与零件批次）")
        if trigger_lots & lots_here and plants_here - trigger_plants:
            contrast_notes.append("异厂同部件批次对照（区分零件批次与装配地）")

        tier = "none"
        reason = ""
        priority = 99
        required_conditions: str | None = None

        if same_source and site_id not in inspected_sites:
            tier = "same_source"
            priority = 1
            required_conditions = triggered[0].required_conditions if triggered else None
            reason = (
                f"同供应批次（{'、'.join(sorted(trigger_lots))}）样品"
                f" {'、'.join(same_source)} 可能已流出且该使用点尚未检查，扩围优先"
            )
            if contrast_notes:
                reason += "；该点同时具备：" + "、".join(contrast_notes)
        elif same_source:
            tier = "same_source"
            priority = 3
            required_conditions = triggered[0].required_conditions if triggered else None
            reason = "同批次样品在场但该点已有检查记录，扩围价值低于未检使用点"
            if contrast_notes:
                reason += "；另有：" + "、".join(contrast_notes)
        elif contrast_notes:
            tier = "contrast"
            priority = 2
            reason = "；".join(contrast_notes)
        else:
            reason = "既无同批次流出样品，也不构成对照，抽查无判别力"

        actions.append(
            PlanAction(
                site_id=site_id,
                site_name=site_name[site_id],
                priority=priority,
                tier=tier,
                reason=reason,
                target_serials=serials_here,
                required_conditions=required_conditions,
                available_slots=remaining,
            )
        )

    actions.sort(key=lambda a: (a.priority, a.site_id))

    # ---- 名额分配：一个实物样品只占一个名额。
    # 只投向有判别力的现场（未检同源点 / 对照点）；已检点的低优先级复看
    # 与“无判别目标”均不占用名额，名额宁可保留。
    assignable = [a for a in actions if a.priority <= 2]
    assigned: list[PlanAction] = []
    budget = remaining_total
    for action in assignable:
        if budget <= 0:
            break
        take = min(budget, action.available_slots)
        if take > 0:
            assigned.append(action)
            budget -= take
    leftover = budget
    if leftover:
        if not assignable:
            leftover_reason = (
                "剩余名额无有判别力的现场可去（身份不明记录不计扩围证据，"
                "不同工况阴性不得宣布撤控）"
            )
        else:
            leftover_reason = (
                "剩余名额保留：具备判别力的现场容量已满，"
                "已检点复看与无判别目标均不应占用名额"
            )
    else:
        leftover_reason = "名额已全部分配"

    # ---- 签收处置
    dispositions: list[ReceiptDisposition] = []
    for receipt in records["receipt"]:
        rtype = receipt["type"]
        if rtype == "实物签收":
            decision = "executed"
            why = "实物已签收，现场名额已使用；即使后续补录改判归属也不退还名额"
            slot_used = True
        else:
            decision = "withdrawable"
            why = "仅为待落实意向、未接收实物，不占用名额；线索改判后可撤回"
            slot_used = False
        dispositions.append(
            ReceiptDisposition(
                receipt_id=receipt["id"],
                site_id=receipt["site"],
                type=rtype,
                item=receipt.get("item"),
                slot_used=slot_used,
                decision=decision,
                reason=why,
            )
        )

    return Plan(
        request_id=request["id"],
        remaining_total=remaining_total,
        hypotheses=hypotheses,
        triggers=triggered,
        assigned=assigned,
        leftover_slots=leftover,
        leftover_reason=leftover_reason,
        candidates=actions,
        receipts=dispositions,
        covered_sites=covered,
    )


# ---------------------------------------------------------------- 总入口
def analyze(
    records: dict[str, Any],
    use_corrections: bool = True,
) -> dict[str, Any]:
    """跑完整分析，返回可 JSON 序列化的结果字典。"""

    parsed = [parse_note(row) for row in records["inspection"]]
    corrections_raw = records.get("correction", []) if use_corrections else []
    corrections = [parse_correction(row, records["logistics"]) for row in corrections_raw]

    attributions, note_serial, invalid_handovers = attribute_notes(
        parsed, records["logistics"], corrections
    )
    samples = build_samples(
        parsed, note_serial, records["assembly"], records["logistics"], invalid_handovers
    )
    families = evaluate_families(parsed, note_serial, samples)

    name_to_id = {s["name"]: s["id"] for s in records["site"]}
    handover_to_site = {
        h["id"]: name_to_id.get(h["to"], h["to"])
        for h in records["logistics"]
    }
    inspected_sites = {
        handover_to_site[a.handover]
        for a in attributions
        if a.kind != "unidentified" and a.handover in handover_to_site
    }
    flows = trace_lot_flow(
        samples,
        records["assembly"],
        records["logistics"],
        invalid_handovers,
        inspected_sites,
        name_to_id,
    )
    plan = build_plan(records, families, flows, samples, attributions, corrections)

    return {
        "parsed_notes": [asdict(p) for p in parsed],
        "corrections": [asdict(c) for c in corrections],
        "attributions": [asdict(a) for a in attributions],
        "samples": [asdict(s) for s in sorted(samples.values(), key=lambda s: s.serial)],
        "unidentified_notes": [
            asdict(a) for a in attributions if a.kind == "unidentified"
        ],
        "families": [asdict(f) for f in families],
        "lot_flow": [asdict(f) for f in flows],
        "plan": asdict(plan),
    }


# ---------------------------------------------------------------- 文本报告
def render_text(result: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("一、现场描述拆解")
    for note in result["parsed_notes"]:
        lines.append(f"  [{note['id']}] 工况：{note['conditions'] or '未注明'}"
                     f"{'（维修后复看）' if note['repair_after'] else ''}")
        for finding in note["findings"]:
            polarity = "阳性" if finding["polarity"] == "positive" else "否定"
            lines.append(f"      · {finding['part']} / {finding['defect']} / {polarity}"
                         f" —— {finding['clause']}")
        serials = "、".join(note["serial_confirmed"]) or "（无完整样号）"
        lines.append(f"      样号：{serials}"
                     + (f"；候选 {'、'.join(note['serial_ambiguous'])}"
                        if note["serial_ambiguous"] else "")
                     + (f"；引用 {'、'.join(note['references'])}"
                        if note["references"] else ""))

    lines.append("")
    lines.append("二、样品归属（独立 / 复看 / 不明）")
    for attr in result["attributions"]:
        label = {
            "independent": "独立样品首证",
            "duplicate": "同一样品复看",
            "unidentified": "身份不明",
            "corrected": "补录落实",
        }[attr["kind"]]
        lines.append(f"  [{attr['note_id']}] {label}"
                     f"；样号 {attr['serial'] or '不能确认'}"
                     f"；候选 {('、'.join(attr['possible_serials']) or '无')}")
        for reason in attr["reasons"]:
            lines.append(f"      - {reason}")

    lines.append("")
    lines.append("三、同源证据与扩围触发")
    for family in result["families"]:
        lines.append(f"  {family['part']}{family['defect']}（基准工况："
                     f"{family['required_conditions'] or '未注明'}）")
        lines.append(f"      独立样品：{('、'.join(family['independent_serials'])) or '无'}"
                     f"；证据笔录：{'、'.join(family['evidence_notes'])}")
        lines.append(f"      扩围触发：{'满足' if family['trigger_met'] else '不满足'}")
        for reason in family["reasons"]:
            lines.append(f"      - {reason}")
        for neg in family["negatives"]:
            lines.append(f"      阴性 [{neg['note_id']}]："
                         f"{'可计入对照' if neg['counts'] else '不能削弱原线索'}（{neg['reason']}）")

    lines.append("")
    lines.append("四、供应批次流向")
    for flow in result["lot_flow"]:
        lines.append(f"  批次 {flow['component_lot']}：样号 {'、'.join(flow['serials'])}")
        lines.append(f"      确认去向：{('、'.join(flow['confirmed_sites'])) or '无'}"
                     f"；可能去向：{('、'.join(flow['possible_sites'])) or '无'}")
        lines.append(f"      未检使用点：{('、'.join(flow['uninspected_sites'])) or '无'}")

    plan = result["plan"]
    lines.append("")
    lines.append("五、下一轮抽查计划"
                 f"（请求 {plan['request_id']}，剩余名额 {plan['remaining_total']}，"
                 f"假设：{'、'.join(plan['hypotheses'])}）")
    for action in plan["candidates"]:
        tier_label = {"same_source": "同源扩围", "contrast": "对照", "none": "无判别目标"}[
            action["tier"]
        ]
        lines.append(f"  [{action['site_id']}]{action['site_name']}：{tier_label}"
                     f"（优先级 {action['priority']}，名额 {action['available_slots']}）")
        lines.append(f"      {action['reason']}")
        if action["required_conditions"]:
            lines.append(f"      抽查工况要求：{action['required_conditions']}")
    lines.append("  分配：" + ("、".join(f"{a['site_id']}({a['site_name']})"
                                     for a in plan["assigned"]) or "无"))
    lines.append(f"  剩余名额 {plan['leftover_slots']}：{plan['leftover_reason']}")

    lines.append("")
    lines.append("六、签收处置")
    for receipt in plan["receipts"]:
        decision = "已执行不可撤回" if receipt["decision"] == "executed" else "尚可撤回"
        lines.append(f"  [{receipt['receipt_id']}] 现场 {receipt['site_id']}"
                     f"/ {receipt['type']} / {receipt['item'] or '无样号'}：{decision}")
        lines.append(f"      {receipt['reason']}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="督察抽查线索分析与抽点计划")
    parser.add_argument("path", type=Path, help="资料文件路径")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而非文本报告")
    parser.add_argument(
        "--no-corrections",
        action="store_true",
        help="忽略质检员补录，展示补录前的归属与计划",
    )
    args = parser.parse_args()
    records = load_records(args.path)
    result = analyze(records, use_corrections=not args.no_corrections)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_text(result))


if __name__ == "__main__":
    main()
