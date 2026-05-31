"""
Tariff Pipeline Orchestrator — Stage 2 §2.3
============================================
LangGraph state graph: retrieve → calculate → END.

Graph topology:
  START → [retrieve] → [calculate] → END
"""

import logging
import sys
from pathlib import Path
from typing import Optional

from langgraph.graph import END, START, StateGraph

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import calculator_agent, retriever_agent
from models.config import TariffPipelineConfig, load_pipeline_config
from models.invoice import TariffInvoice
from models.vessel import VesselInput

from .state import TariffPipelineState

log = logging.getLogger(__name__)


def build_pipeline(tariff_store, chunk_store=None):
    """Build and compile the tariff calculation LangGraph pipeline."""

    def node_retrieve(state: TariffPipelineState) -> dict:
        vessel = state["vessel"]
        log.info(f"[pipeline] retrieve  port={vessel.port}  GT={vessel.gross_tonnage}")
        try:
            result = retriever_agent.run(vessel, tariff_store, chunk_store)
            log.info(f"[pipeline] retrieve done — {len(result.applicable_fees)} applicable  {len(result.not_applicable)} skipped  {len(result.human_review_items)} flagged")
            return {
                "applicable_fees":    result.applicable_fees,
                "not_applicable":     result.not_applicable,
                "human_review_items": result.human_review_items,
                "step_log": [f"retrieve: {len(result.applicable_fees)} applicable, {len(result.not_applicable)} skipped"],
            }
        except Exception as exc:
            log.error(f"[pipeline] retrieve FAILED: {exc}")
            return {"applicable_fees": [], "not_applicable": [], "human_review_items": [], "errors": [f"retrieve: {exc}"], "step_log": ["retrieve: FAILED"]}

    def node_calculate(state: TariffPipelineState) -> dict:
        vessel = state["vessel"]
        log.info(f"[pipeline] calculate  {len(state['applicable_fees'])} fees")
        try:
            invoice = calculator_agent.run(
                vessel=vessel,
                applicable_fees=state["applicable_fees"],
                not_applicable=state["not_applicable"],
                human_review_items=state["human_review_items"],
            )
            log.info(f"[pipeline] calculate done — {len(invoice.line_items)} items  subtotal={invoice.subtotal:,.2f}")
            return {"invoice": invoice, "step_log": [f"calculate: {len(invoice.line_items)} items  subtotal={invoice.subtotal:,.2f}"]}
        except Exception as exc:
            log.error(f"[pipeline] calculate FAILED: {exc}")
            return {"invoice": None, "errors": [f"calculate: {exc}"], "step_log": ["calculate: FAILED"]}

    builder = StateGraph(TariffPipelineState)
    builder.add_node("retrieve",  node_retrieve)
    builder.add_node("calculate", node_calculate)
    builder.add_edge(START,       "retrieve")
    builder.add_edge("retrieve",  "calculate")
    builder.add_edge("calculate", END)
    return builder.compile()


def run_pipeline(vessel: VesselInput, tariff_store, chunk_store=None, config: Optional[TariffPipelineConfig] = None) -> TariffInvoice:
    """Build, run, and return the invoice in one call."""
    pipeline = build_pipeline(tariff_store, chunk_store)
    initial: dict = {
        "vessel":             vessel,
        "config":             config or load_pipeline_config(vessel.country),
        "applicable_fees":    [],
        "not_applicable":     [],
        "human_review_items": [],
        "invoice":            None,
        "errors":             [],
        "step_log":           [],
    }
    final_state = pipeline.invoke(initial)

    if final_state.get("errors"):
        log.warning(f"Pipeline completed with errors: {final_state['errors']}")
    log.info(f"Step log: {' → '.join(final_state.get('step_log', []))}")

    invoice = final_state.get("invoice")
    if invoice is None:
        raise RuntimeError("Pipeline completed but produced no invoice — check errors above")
    return invoice


# ─── Entry Point (standalone) ────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import json
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")

    from agents.document_preparation_agent import TariffStore, db_path_for_version, load_active_version
    from agents.hierarchical_chunk_agent import CHUNK_DB_PATH, ChunkStore

    parser = argparse.ArgumentParser(description="Tariff Pipeline — §2.3")
    parser.add_argument("--country",        default="South Africa", help="Country, e.g. 'South Africa'")
    parser.add_argument("--port",           required=True)
    parser.add_argument("--gt",             type=float, required=True)
    parser.add_argument("--vessel-type",    default="")
    parser.add_argument("--voyage",         default="inbound", choices=["inbound", "outbound"])
    parser.add_argument("--after-hours",    action="store_true")
    parser.add_argument("--public-holiday", action="store_true")
    parser.add_argument("--in-ballast",     action="store_true")
    parser.add_argument("--cargo-type",     default="")
    parser.add_argument("--condition",      action="append", default=[], dest="conditions")
    parser.add_argument("--version-tag",    default=None)
    args = parser.parse_args()

    version_tag = args.version_tag or load_active_version(args.country)
    if not version_tag:
        raise SystemExit(f"No active version for country '{args.country}'. Pass --version-tag.")

    vessel_input = VesselInput(
        country=args.country,
        port=args.port,
        gross_tonnage=args.gt,
        vessel_type=args.vessel_type,
        voyage_type=args.voyage,
        cargo_type=args.cargo_type,
        after_hours=args.after_hours,
        public_holiday=args.public_holiday,
        in_ballast=args.in_ballast,
        special_conditions=args.conditions,
    )

    cfg     = TariffPipelineConfig(country=args.country, tariff_version=version_tag)
    t_store = TariffStore(cfg.resolved_db_path)
    c_store = ChunkStore(CHUNK_DB_PATH) if CHUNK_DB_PATH.exists() else None
    try:
        invoice = run_pipeline(vessel_input, t_store, c_store, config=cfg)
        print(json.dumps(invoice.model_dump(), indent=2))
    finally:
        t_store.close()
        if c_store:
            c_store.close()
