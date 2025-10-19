import os
import json
from typing import List, Dict, Any
from dataclasses import dataclass

from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import requests

# ------------------------------------------------------------------------------
# Config
# ------------------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# Use a widely available model/version pair
GEMINI_TEXT_URL = "https://generativelanguage.googleapis.com/v1/models/gemini-2.5-flash:generateContent"
# ------------------------------------------------------------------------------
# Flask app (serves UI + API)
# ------------------------------------------------------------------------------
app = Flask(__name__, template_folder="templates")
# Allow all origins in dev; optional since we render UI from same origin
CORS(app, resources={r"/*": {"origins": "*"}})

# ------------------------------------------------------------------------------
# Data shapes
# ------------------------------------------------------------------------------
@dataclass
class GenomicVariant:
    gene: str
    rsid: str
    genotype: str

@dataclass
class PlanRequest:
    age: int
    sex: str
    height_cm: float
    weight_kg: float
    activity_level: str
    goal: str
    cuisine_pref: str
    dietary_pref: str
    budget_level: str
    constraints: List[str]
    genomics: List[GenomicVariant]

# ------------------------------------------------------------------------------
# Traits (illustrative)
# ------------------------------------------------------------------------------
def derive_traits(genomics: List[GenomicVariant]) -> Dict[str, str]:
    traits = {
        "fat_sensitivity": "neutral",
        "carb_tolerance": "moderate",
        "methylation_support": "standard",
        "lactose_tolerance": "unknown",
        "caffeine_sensitivity": "unknown",
    }
    for v in genomics:
        g = v.gene.upper().strip()
        rs = v.rsid.lower().strip()
        gt = v.genotype.upper().replace("/", "").strip()
        if g == "APOA2" and rs == "rs5082" and gt == "AA":
            traits["fat_sensitivity"] = "higher"
        if g == "TCF7L2":
            traits["carb_tolerance"] = "lower"
        if g == "FTO":
            traits["carb_tolerance"] = traits.get("carb_tolerance", "moderate") or "moderate"
        if g == "MTHFR" and rs == "rs1801133" and any(a in gt for a in ["TT", "CT"]):
            traits["methylation_support"] = "elevated"
        if g == "LCT":
            traits["lactose_tolerance"] = "lower"
        if g == "CYP1A2":
            traits["caffeine_sensitivity"] = "higher"
    return traits

# ------------------------------------------------------------------------------
# Prompt
# ------------------------------------------------------------------------------
def build_prompt(req: PlanRequest, traits: Dict[str, str]) -> str:
    constraints_text = ", ".join(req.constraints) if req.constraints else "none"
    return f"""
Return STRICT JSON only (no prose). Create a 7-day genomics-informed nutrition plan.

User profile:
- Age: {req.age}, Sex: {req.sex}, Height: {req.height_cm} cm, Weight: {req.weight_kg} kg
- Activity level: {req.activity_level}
- Goal: {req.goal}
- Cuisine: {req.cuisine_pref}
- Dietary preference: {req.dietary_pref}
- Budget: {req.budget_level}
- Constraints: {constraints_text}

Genomics-derived traits (non-diagnostic):
{json.dumps(traits)}

Output JSON schema:
{{
  "plan_overview": {{
    "kcal_target": <number>,
    "macros": {{ "carbs_g": <number>, "protein_g": <number>, "fat_g": <number> }},
    "notes": "<short string>"
  }},
  "days": [
    {{
      "day": 1,
      "meals": [
        {{ "name": "Breakfast", "items": ["<food1>", "<food2>"], "notes": "<short note>" }},
        {{ "name": "Lunch", "items": ["<food1>", "<food2>"], "notes": "<short note>" }},
        {{ "name": "Dinner", "items": ["<food1>", "<food2>"], "notes": "<short note>" }}
      ],
      "snacks": ["<snack1>", "<snack2>"],
      "rationale": "<1-2 sentences tying choices to traits and goal>"
    }}
    ... days 2 to 7 ...
  ],
  "shopping_list": ["<item1>", "<item2>", "..."],
  "guidelines": [
    "If fat_sensitivity is higher, reduce SFA; prefer MUFA/PUFA.",
    "If carb_tolerance is lower, use lower-GI carbs (millets, legumes).",
    "If methylation_support is elevated, add folate-rich foods.",
    "If lactose_tolerance is lower, provide lactose-free swaps.",
    "Scale portions to activity and budget."
  ]
}}

Rules:
- Honor dietary preference and cuisine.
- Keep notes concise. Portion sizes scalable.
- Return ONLY the JSON object.
"""

# ------------------------------------------------------------------------------
# Gemini call
# ------------------------------------------------------------------------------
def call_gemini_text(prompt: str) -> str:
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY not configured")
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY,
    }
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        r = requests.post(GEMINI_TEXT_URL, json=payload, headers=headers, timeout=60)
    except requests.RequestException as ex:
        raise RuntimeError(f"Upstream request error: {ex}") from ex
    if r.status_code != 200:
        raise RuntimeError(r.text)
    data = r.json()
    text = ""
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        pass
    if not text:
        raise RuntimeError("Empty response from Gemini")
    return text

# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------
def parse_plan_request(payload: Dict[str, Any]) -> PlanRequest:
    genomics = [GenomicVariant(**g) for g in payload.get("genomics", [])]
    constraints = payload.get("constraints", [])
    if isinstance(constraints, str):
        constraints = [c.strip() for c in constraints.split(",") if c.strip()]
    return PlanRequest(
        age=int(payload["age"]),
        sex=str(payload["sex"]).strip(),
        height_cm=float(payload["height_cm"]),
        weight_kg=float(payload["weight_kg"]),
        activity_level=str(payload["activity_level"]).strip(),
        goal=str(payload["goal"]).strip(),
        cuisine_pref=str(payload.get("cuisine_pref", "Indian")).strip(),
        dietary_pref=str(payload.get("dietary_pref", "flexitarian")).strip(),
        budget_level=str(payload.get("budget_level", "medium")).strip(),
        constraints=constraints,
        genomics=genomics,
    )

def extract_first_json_block(text: str) -> str:
    t = text.strip()
    if t.startswith("{") and t.endswith("}"):
        return t
    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = t[start:end+1]
        try:
            json.loads(candidate)
            return candidate
        except Exception:
            return t
    return t

# ------------------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def index():
    # Render your UI; the template includes the same HTML and JS you provided
    return render_template("index.html")

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "model_url": GEMINI_TEXT_URL, "has_key": bool(GEMINI_API_KEY)})

@app.route("/generate-plan", methods=["POST"])
def generate_plan():
    try:
        payload = request.get_json(force=True, silent=False)
        req = parse_plan_request(payload)
        traits = derive_traits(req.genomics)
        prompt = build_prompt(req, traits)
        plan_text = call_gemini_text(prompt)
        plan_text = extract_first_json_block(plan_text)
        return jsonify({"traits": traits, "plan_text": plan_text})
    except ValueError as ve:
        return jsonify({"detail": str(ve)}), 400
    except RuntimeError as re:
        return jsonify({"detail": str(re)}), 502
    except Exception as ex:
        return jsonify({"detail": f"Server error: {ex}"}), 500

# ------------------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    print("Using Gemini endpoint:", GEMINI_TEXT_URL)
    print("Has API key:", bool(GEMINI_API_KEY))
    # Bind on 0.0.0.0 so LAN/mobile can access if needed
    app.run(host="0.0.0.0", port=5000, debug=True)
