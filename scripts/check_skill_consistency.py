#!/usr/bin/env python3
"""Skill-drift consistency checker.

Verifies that distributed REI skills (.skill/.plugin ZIPs or unpacked skill
directories) state the same domain constants as the SiftStack source code.
Source-of-truth values are parsed LIVE from src/*.py at runtime, so the
checker never goes stale when code constants change.

Usage:
    python scripts/check_skill_consistency.py                       # checks "Skills for REI/"
    python scripts/check_skill_consistency.py path/to/skills_dir    # any dir of ZIPs/folders
    python scripts/check_skill_consistency.py foo.skill bar.plugin  # explicit files

Exit code 0 = clean, 1 = mismatch or credential/path leak found.

Checks per skill (only where the skill mentions the concept — silent skip otherwise):
  * Comp adjustments: $/sqft, bedroom, bathroom, year-built (src/comp_analyzer.py)
  * Financing defaults: HML rate + points, conventional rate, closing % ,
    MAO flip/wholesale rules (src/deal_analyzer.py)
  * DOD sanity gap years (src/obituary_enricher.py)
  * Phone tier boundaries (src/phone_validator.py)
  * Leaks: hardcoded credentials, absolute /home|/Users paths
"""

from __future__ import annotations

import io
import re
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"


# ── Source-of-truth extraction ────────────────────────────────────────

def _grab(path: Path, pattern: str) -> float | None:
    m = re.search(pattern, path.read_text(errors="replace"))
    return float(m.group(1)) if m else None


def load_source_values() -> dict[str, float]:
    comp = SRC / "comp_analyzer.py"
    deal = SRC / "deal_analyzer.py"
    obit = SRC / "obituary_enricher.py"
    vals = {
        "per_sqft":      _grab(comp, r"ADJ_PER_SQFT\s*=\s*([\d.]+)"),
        "bedroom":       _grab(comp, r"ADJ_PER_BEDROOM\s*=\s*([\d.]+)"),
        "bathroom":      _grab(comp, r"ADJ_PER_BATHROOM\s*=\s*([\d.]+)"),
        "year_built":    _grab(comp, r"ADJ_PER_YEAR_BUILT\s*=\s*([\d.]+)"),
        "hml_rate":      _grab(deal, r"DEFAULT_HARD_MONEY_RATE\s*=\s*([\d.]+)"),
        "hml_points":    _grab(deal, r"DEFAULT_HARD_MONEY_POINTS\s*=\s*([\d.]+)"),
        "conv_rate":     _grab(deal, r"DEFAULT_CONVENTIONAL_RATE\s*=\s*([\d.]+)"),
        "closing_pct":   _grab(deal, r"DEFAULT_CLOSING_COSTS_PCT\s*=\s*([\d.]+)"),
        "flip_rule":     _grab(deal, r"DEFAULT_FLIP_RULE\s*=\s*([\d.]+)"),
        "wholesale_rule": _grab(deal, r"DEFAULT_WHOLESALE_RULE\s*=\s*([\d.]+)"),
        "dod_gap":       _grab(obit, r"MAX_DOD_GAP_YEARS\s*=\s*([\d.]+)"),
    }
    missing = [k for k, v in vals.items() if v is None]
    if missing:
        print(f"WARNING: could not parse source constants: {missing}")
    return vals


# Phone tiers: (label, low, high) parsed from phone_validator.py
def load_phone_tiers() -> list[tuple[str, int, int]]:
    text = (SRC / "phone_validator.py").read_text(errors="replace")
    return [(m.group(1), int(m.group(2)), int(m.group(3)))
            for m in re.finditer(r'"(Dial \w+|Drop)":\s*\((\d+),\s*(\d+)\)', text)]


# ── Skill text collection ─────────────────────────────────────────────

TEXT_EXT = {".md", ".txt", ".json", ".py", ".js", ".yaml", ".yml", ".csv"}


def iter_skill_texts(target: Path):
    """Yield (name, text) for every text member of a ZIP or directory."""
    if target.is_dir():
        for p in target.rglob("*"):
            if p.is_file() and p.suffix.lower() in TEXT_EXT:
                yield str(p.relative_to(target)), p.read_text(errors="replace")
    elif zipfile.is_zipfile(target):
        with zipfile.ZipFile(target) as z:
            for info in z.infolist():
                if Path(info.filename).suffix.lower() in TEXT_EXT:
                    yield info.filename, io.TextIOWrapper(
                        z.open(info), errors="replace").read()


# ── Checks ────────────────────────────────────────────────────────────

def _num(s: str) -> float:
    return float(s.replace(",", ""))


def check_text(name: str, member: str, text: str, sv: dict,
               tiers: list) -> list[str]:
    errs: list[str] = []

    def near(concept_re: str):
        """Find 'concept ... $value' within 60 chars, return list of (match, num)."""
        pattern = (r"(?:" + concept_re + r")[^.\n]{0,60}?"
                   r"\$\s*([\d,]+(?:\.\d+)?)\s*([Kk]\b)?")
        out = []
        for m in re.finditer(pattern, text, re.I):
            n = _num(m.group(1))
            if m.group(2):
                n *= 1000
            out.append((m.group(0)[:80], n))
        return out

    # Comp adjustments (accept $5,000 / $5000 / $5K forms)
    dollar_checks = [
        (r"per\s+bedroom|bedroom\s+adjust", sv["bedroom"], ("5000", "5,000", "5K", "$5k")),
        (r"per\s+(?:full\s+)?bathroom|bathroom\s+adjust", sv["bathroom"], ("7500", "7,500", "7.5K")),
        (r"per\s+sq\.?\s*ft|\$/sq", sv["per_sqft"], ("85",)),
        (r"per\s+year\s+of\s+age|year[- ]built\s+adjust|age\s+adjust", sv["year_built"], ("500",)),
    ]
    for concept, src_val, _ in dollar_checks:
        if src_val is None:
            continue
        for snippet, n in near(concept):
            if n != src_val:
                errs.append(f"{member}: '{snippet}' != source {src_val:g}")

    # Percent-style values
    pct_checks = [
        (r"hard[- ]money[^.\n]{0,30}?(?:rate|interest)[^.\n]{0,20}?(\d+(?:\.\d+)?)\s*%|(?<![\d.])(\d+(?:\.\d+)?)\s*%\s+hard[- ]money",
         sv["hml_rate"] * 100 if sv["hml_rate"] else None, "HML rate"),
        (r"conventional[^.\n]{0,30}?(?:rate|interest)[^.\n]{0,20}?(\d+(?:\.\d+)?)\s*%|(?<![\d.])(\d+(?:\.\d+)?)\s*%\s+conventional",
         sv["conv_rate"] * 100 if sv["conv_rate"] else None, "conventional rate"),
        (r"closing\s+costs?[^.\n]{0,40}?(\d+(?:\.\d+)?)\s*%", sv["closing_pct"] * 100 if sv["closing_pct"] else None, "closing costs"),
        (r"(\d+)\s*%\s*rule[^.\n]{0,30}(?:flip|MAO)", sv["flip_rule"] * 100 if sv["flip_rule"] else None, "flip rule"),
        (r"(\d+)\s*%\s*rule[^.\n]{0,30}wholesale", sv["wholesale_rule"] * 100 if sv["wholesale_rule"] else None, "wholesale rule"),
    ]
    for pattern, src_val, label in pct_checks:
        if src_val is None:
            continue
        for m in re.finditer(pattern, text, re.I):
            raw = next((g for g in m.groups() if g), None)
            if raw and abs(_num(raw) - src_val) > 0.01:
                errs.append(f"{member}: {label} '{m.group(0)[:60]}' != source {src_val:g}%")

    # HML points
    if sv.get("hml_points") is not None:
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:origination\s+)?points?", text, re.I):
            ctx = text[max(0, m.start() - 60):m.start()].lower()
            if "hard money" in ctx or "hml" in ctx:
                if _num(m.group(1)) != sv["hml_points"]:
                    errs.append(f"{member}: HML points '{m.group(0)}' != source {sv['hml_points']:g}")

    # DOD gap
    if sv.get("dod_gap") is not None:
        for m in re.finditer(r"(\d+)\s*years?[^.\n]{0,40}(?:before|prior)[^.\n]{0,40}(?:filing|notice)", text, re.I):
            if _num(m.group(1)) != sv["dod_gap"]:
                errs.append(f"{member}: DOD gap '{m.group(0)[:60]}' != source {sv['dod_gap']:g}yr")

    # Phone tier boundaries (e.g. "81-100" must map to Dial First)
    tier_map = {(lo, hi): label for label, lo, hi in tiers}
    for m in re.finditer(r"(\d{1,3})\s*[-–]\s*(\d{1,3})[^.\n]{0,30}(Dial \w+|Drop)", text, re.I):
        lo, hi, label = int(m.group(1)), int(m.group(2)), m.group(3)
        expected = tier_map.get((lo, hi))
        if tier_map and expected and expected.lower() != label.lower():
            errs.append(f"{member}: tier '{m.group(0)[:60]}' != source ({lo}-{hi} = {expected})")
        elif tier_map and (lo, hi) not in tier_map and label.lower().startswith(("dial", "drop")):
            errs.append(f"{member}: tier range {lo}-{hi} not in source tiers {sorted(tier_map)}")

    # Credential leaks
    for m in re.finditer(r"(password|passwd|api[_-]?key|token)\s*[:=]\s*['\"]?[A-Za-z0-9!@#$%^&*_-]{6,}", text, re.I):
        val = m.group(0)
        if not re.search(r"(YOUR|EXAMPLE|PLACEHOLDER|<|xxx|\{\{|\$\{?[A-Z_]|os\.environ|env\[)", val, re.I):
            errs.append(f"{member}: possible hardcoded credential: '{val[:50]}...'")
    for m in re.finditer(r"[\w.+-]+@[\w-]+\.\w+", text):
        if not m.group(0).endswith(("example.com", "domain.com", "email.com")):
            ctx = text[max(0, m.start() - 40):m.start()].lower()
            if "login" in ctx or "password" in ctx or "credential" in ctx:
                errs.append(f"{member}: email near credentials: {m.group(0)}")

    # Absolute path leaks
    for m in re.finditer(r"(/home/\w+/|/Users/\w+/)[^\s'\")]*", text):
        errs.append(f"{member}: absolute path leak: {m.group(0)[:60]}")

    return errs


# ── Main ──────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    targets: list[Path] = []
    args = [Path(a) for a in argv] or [REPO / "Skills for REI"]
    for a in args:
        if a.is_file():
            targets.append(a)
        elif a.is_dir():
            zips = sorted(list(a.rglob("*.skill")) + list(a.rglob("*.plugin")))
            # unpacked skill dirs = any dir directly containing SKILL.md
            dirs = sorted({p.parent for p in a.rglob("SKILL.md")})
            targets.extend(zips or [])
            targets.extend(d for d in dirs if not any(z in d.parents for z in []))
        else:
            print(f"WARNING: {a} not found, skipping")

    if not targets:
        print("No .skill/.plugin files or SKILL.md dirs found.")
        return 1

    sv = load_source_values()
    tiers = load_phone_tiers()
    total_errs = 0
    for t in targets:
        errs: list[str] = []
        for member, text in iter_skill_texts(t):
            errs.extend(check_text(t.name, member, text, sv, tiers))
        status = "OK " if not errs else "DRIFT"
        print(f"[{status}] {t}")
        for e in errs:
            print(f"    - {e}")
        total_errs += len(errs)

    print(f"\n{len(targets)} skill(s) checked, {total_errs} issue(s).")
    return 1 if total_errs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
