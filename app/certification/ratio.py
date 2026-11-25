from app.certification.engine import committed_ratio, evaluate_certification

calculate_ratio = committed_ratio
evaluate_ratios = evaluate_certification

__all__ = ["committed_ratio", "calculate_ratio", "evaluate_certification", "evaluate_ratios"]
