"""
prioritizer.py - AI/ML Asset Criticality Index (ACI) & Prioritization Engine
Computes multi-dimensional risk scores across TMS, SMMS, and TDMS maintenance requests.
"""

from typing import List, Dict, Any
from .ml_degradation_model import AssetDegradationML

class AssetCriticalityPrioritizer:
    """
    Two-stage AI prioritization engine:
    1. Machine Learning (Stage 1): Trained RandomForest and GradientBoosting models
       predict asset in-service failure probability and downstream train delay cascade.
    2. Operational Risk Synthesis (Stage 2): Synthesizes ML outputs with Indian Railways
       safety weights, permanent speed restrictions (PSR), and overdue hazard curves.
    """

    def __init__(self,
                 w_safety: float = 0.40,
                 w_ml_risk: float = 0.30,
                 w_psr: float = 0.20,
                 w_overdue: float = 0.10):
        self.w_safety = w_safety
        self.w_ml_risk = w_ml_risk
        self.w_psr = w_psr
        self.w_overdue = w_overdue
        self.ml_engine = AssetDegradationML()
        try:
            self.ml_engine.load()
        except Exception:
            pass

    def compute_aci(self, item: Dict[str, Any]) -> float:
        # 1. Safety component [0 to 1]
        safety = float(item.get("safety_weight", 0.5))
        if item.get("severity") == "CRITICAL":
            safety = max(safety, 0.95)

        # 2. Machine Learning Predictive Failure & Delay Risk [0 to 1]
        try:
            ml_pred = self.ml_engine.predict_defect_risk(item)
            ml_fail_prob = ml_pred.get("ml_failure_prob_7d", 0.5)
            ml_delay_norm = min(1.0, ml_pred.get("ml_predicted_delay_mins", 30.0) / 90.0)
            ml_risk_score = 0.65 * ml_fail_prob + 0.35 * ml_delay_norm
            item["ml_telemetry"] = ml_pred
        except Exception:
            ml_risk_score = 0.5
            item["ml_telemetry"] = {"ml_failure_prob_7d": 0.5, "ml_predicted_delay_mins": 30.0}

        # 3. Speed restriction impact [0 to 1]
        psr = item.get("psr_speed_kmph")
        if psr and psr > 0:
            psr_impact = max(0.0, min(1.0, (100.0 - psr) / 100.0))
        else:
            psr_impact = 0.0

        # 4. Days overdue hazard curve [0 to 1]
        overdue = float(item.get("days_overdue", 0))
        target_days = max(1.0, float(item.get("target_completion_days", 7)))
        ratio = overdue / target_days
        overdue_score = min(1.0, ratio * 0.6 + (0.05 * (overdue ** 1.3)))

        # Composite score scaled to 0-100 blending ML predictions
        raw_score = (
            self.w_safety * safety +
            self.w_ml_risk * ml_risk_score +
            self.w_psr * psr_impact +
            self.w_overdue * overdue_score
        )
        
        # Boost critical safety defects to ensure top tier
        if item.get("severity") == "CRITICAL" and raw_score < 0.75:
            raw_score = 0.82

        return round(raw_score * 100.0, 1)

    def classify_tier(self, aci_score: float, severity: str) -> str:
        if aci_score >= 75.0 or severity == "CRITICAL":
            return "P1_CRITICAL"
        elif aci_score >= 50.0:
            return "P2_URGENT"
        else:
            return "P3_ROUTINE"

    def rank_maintenance_demands(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Computes ACI and sorts items descending by criticality."""
        ranked = []
        for it in items:
            item_copy = dict(it)
            score = self.compute_aci(item_copy)
            tier = self.classify_tier(score, item_copy.get("severity", "ROUTINE"))
            item_copy["aci_score"] = score
            item_copy["priority_tier"] = tier
            ranked.append(item_copy)

        ranked.sort(key=lambda x: x["aci_score"], reverse=True)
        return ranked

if __name__ == "__main__":
    from Backend.data_ingestion.tms_data import generate_tms_defects
    from Backend.data_ingestion.smms_data import generate_smms_defects
    from Backend.data_ingestion.tdms_data import generate_tdms_defects

    prioritizer = AssetCriticalityPrioritizer()
    tms = generate_tms_defects(5)
    smms = generate_smms_defects(5)
    tdms = generate_tdms_defects(5)

    all_defects = prioritizer.rank_maintenance_demands(tms + smms + tdms)
    print("Top 3 Critical Assets:")
    for d in all_defects[:3]:
        print(f"[{d['priority_tier']}] ACI: {d['aci_score']} | Dept: {d['department']} | Type: {d['defect_type']} | Sec: {d['section']} {d['track_id']}")
