"""Stock Forecasting Agent — informational research, not investment advice."""

from .models import DISCLAIMER, AnalysisRequest, AnalysisResult, Horizon
from .pipeline import run_analysis

__version__ = "0.1.0"
__all__ = ["DISCLAIMER", "AnalysisRequest", "AnalysisResult", "Horizon", "run_analysis"]
