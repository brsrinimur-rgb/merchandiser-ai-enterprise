"""
Approval & Decision Workflow (v4).

Bridges the Capital Optimizer's recommendations into a governed pipeline:

    AI Recommendation -> Merchandiser Review -> Buyer Review -> Final Approval -> PO Ready
                                             \\-> Rejected (from any stage)

Nothing in analytics.py (sales intelligence, replenishment, the capital
optimizer, etc.) ever writes to the database -- those are all pure
recommendations recomputed from the metrics frame. This module is what turns
a recommendation into a tracked, auditable decision: every edit and every
stage transition is logged as an immutable DecisionHistory row (actor,
timestamp, previous/new qty, reason, OTB impact), and reaching "po_ready"
creates a real PurchaseOrder row, closing the loop back into the same data
the replenishment engine reads.

There is no authentication yet (see README) -- "actor" is a free-text
name/handle passed by the caller, not a verified identity.
"""
import datetime as dt

from sqlalchemy import func

from models import PurchaseDecision, DecisionHistory, PurchaseOrder, Item, Store

NEXT_STATUS = {
    "pending_merchandiser": "pending_buyer",
    "pending_buyer": "pending_final",
    "pending_final": "po_ready",
}

STAGE_FOR_STATUS = {
    "pending_merchandiser": "merchandiser",
    "pending_buyer": "buyer",
    "pending_final": "final",
}

ALL_STATUSES = ["pending_merchandiser", "pending_buyer", "pending_final", "po_ready", "rejected"]


class WorkflowError(Exception):
    pass


def _now():
    return dt.datetime.utcnow()


# --------------------------------------------------------------------------
# Bringing AI recommendations into the queue
# --------------------------------------------------------------------------

def sync_recommendations(db, capital_optimizer_result):
    """
    Pull the Capital Optimizer's current recommendations into the workflow
    queue. Idempotent: a store/item that already has an *open* (non-terminal)
    decision is left untouched -- re-running the AI engine should never
    silently overwrite a merchandiser's edit or a rejection. Only
    store/items with no open decision, and recommended_po_qty > 0, get a new
    queue entry, seeded at pending_merchandiser with the AI's own quantity.
    """
    existing_open = {
        (d.store_id, d.item_id)
        for d in db.query(PurchaseDecision).filter(~PurchaseDecision.status.in_(["rejected", "po_ready"]))
    }

    created = 0
    now = _now()
    for rec in capital_optimizer_result["recommendations"]:
        if rec["recommended_po_qty"] <= 0:
            continue
        key = (rec["store_id"], rec["item_id"])
        if key in existing_open:
            continue

        decision = PurchaseDecision(
            store_id=rec["store_id"], item_id=rec["item_id"], category=rec["category"],
            ai_recommended_qty=rec["recommended_po_qty"], ai_recommended_value=rec["recommended_po_value"],
            current_qty=rec["recommended_po_qty"], current_value=rec["recommended_po_value"],
            source_exception_flag=rec["exception_flag"], is_emergency="true" if rec["emergency"] else "false",
            status="pending_merchandiser", otb_impact_value=rec["recommended_po_value"],
            created_at=now, updated_at=now,
        )
        db.add(decision)
        db.flush()  # populate decision.id before logging history
        db.add(DecisionHistory(
            decision_id=decision.id, actor="AI Engine", stage="system", action="created",
            from_status=None, to_status="pending_merchandiser",
            previous_qty=None, new_qty=decision.current_qty,
            reason=f"Capital Optimizer recommendation ({rec['exception_flag'] or 'within budget'})",
            otb_impact_value=decision.otb_impact_value, timestamp=now,
        ))
        created += 1

    db.commit()
    return {"created": created, "already_in_workflow": len(existing_open)}


# --------------------------------------------------------------------------
# Queue reads
# --------------------------------------------------------------------------

def decision_to_dict(d):
    return {
        "id": d.id,
        "store_id": d.store_id, "store_name": d.store.name if d.store else None,
        "item_id": d.item_id, "item_code": d.item.item_code if d.item else None,
        "item_name": d.item.item_name if d.item else None,
        "category": d.category,
        "ai_recommended_qty": d.ai_recommended_qty, "ai_recommended_value": d.ai_recommended_value,
        "current_qty": d.current_qty, "current_value": d.current_value,
        "qty_edited": d.current_qty != d.ai_recommended_qty,
        "source_exception_flag": d.source_exception_flag,
        "is_emergency": d.is_emergency == "true",
        "status": d.status, "stage": STAGE_FOR_STATUS.get(d.status),
        "otb_impact_value": d.otb_impact_value,
        "po_number": d.po_number,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
    }


def history_to_dict(h):
    return {
        "id": h.id, "decision_id": h.decision_id, "actor": h.actor, "stage": h.stage, "action": h.action,
        "from_status": h.from_status, "to_status": h.to_status,
        "previous_qty": h.previous_qty, "new_qty": h.new_qty,
        "reason": h.reason, "otb_impact_value": h.otb_impact_value,
        "timestamp": h.timestamp.isoformat() if h.timestamp else None,
    }


def list_decisions(db, status=None):
    q = db.query(PurchaseDecision)
    if status:
        q = q.filter(PurchaseDecision.status == status)
    return [decision_to_dict(d) for d in q.order_by(PurchaseDecision.updated_at.desc()).all()]


def get_decision(db, decision_id):
    return db.query(PurchaseDecision).filter(PurchaseDecision.id == decision_id).first()


def get_history(db, decision_id):
    rows = (
        db.query(DecisionHistory)
        .filter(DecisionHistory.decision_id == decision_id)
        .order_by(DecisionHistory.timestamp)
        .all()
    )
    return [history_to_dict(h) for h in rows]


def workflow_summary(db):
    rows = (
        db.query(PurchaseDecision.status, func.count(PurchaseDecision.id), func.sum(PurchaseDecision.current_value))
        .group_by(PurchaseDecision.status)
        .all()
    )
    summary = {status: {"count": count, "value": round(float(value or 0), 2)} for status, count, value in rows}
    for s in ALL_STATUSES:
        summary.setdefault(s, {"count": 0, "value": 0.0})
    return summary


# --------------------------------------------------------------------------
# Actions: edit / approve / reject at whichever stage a decision currently sits
# --------------------------------------------------------------------------

def apply_action(db, decision_id, stage, action, actor, qty=None, reason=None):
    """
    stage:  "merchandiser" | "buyer" | "final" -- must match the decision's
            current stage (derived from its status) or this raises.
    action: "edit" | "approve" | "reject" -- edit is not valid at "final"
            (final approval is a checkpoint, not another edit point).
    """
    if not actor or not actor.strip():
        raise WorkflowError("An actor name is required for every action (no auth system yet -- see README)")

    decision = get_decision(db, decision_id)
    if not decision:
        raise WorkflowError(f"Decision {decision_id} not found")
    if decision.status in ("rejected", "po_ready"):
        raise WorkflowError(f"Decision is already '{decision.status}' -- no further action is possible")

    expected_stage = STAGE_FOR_STATUS.get(decision.status)
    if expected_stage != stage:
        raise WorkflowError(
            f"Decision is currently at the '{expected_stage}' stage (status={decision.status}); "
            f"cannot apply a '{stage}' action to it"
        )
    if action not in ("edit", "approve", "reject"):
        raise WorkflowError(f"Unknown action '{action}'")
    if stage == "final" and action == "edit":
        raise WorkflowError("Final approval is a checkpoint, not an edit point -- reject and re-route if the qty needs to change")
    if action in ("reject",) and not reason:
        raise WorkflowError("A reason is required to reject a decision")

    now = _now()
    from_status = decision.status
    item = db.query(Item).filter(Item.id == decision.item_id).first()
    cost = item.cost if item else 0
    prev_qty = decision.current_qty

    if action == "edit":
        if qty is None or qty < 0:
            raise WorkflowError("Edit requires a non-negative quantity")
        decision.current_qty = int(qty)
        decision.current_value = round(decision.current_qty * (cost or 0), 2)
        decision.otb_impact_value = decision.current_value
        to_status = decision.status  # an edit doesn't move the stage on its own
    elif action == "approve":
        to_status = NEXT_STATUS[decision.status]
        decision.status = to_status
    else:  # reject
        to_status = "rejected"
        decision.status = to_status

    decision.updated_at = now
    db.add(DecisionHistory(
        decision_id=decision.id, actor=actor.strip(), stage=stage, action=action,
        from_status=from_status, to_status=to_status,
        previous_qty=prev_qty, new_qty=decision.current_qty,
        reason=reason, otb_impact_value=decision.otb_impact_value, timestamp=now,
    ))

    if to_status == "po_ready":
        po_number = f"WF-{decision.id:05d}"
        decision.po_number = po_number
        db.add(PurchaseOrder(
            po_number=po_number, supplier_id=item.supplier_id if item else None,
            item_id=decision.item_id, store_id=decision.store_id,
            ordered_qty=decision.current_qty, received_qty=0, balance_qty=decision.current_qty,
            order_date=now.date(), eta=None, received_date=None, status="open",
        ))
        db.add(DecisionHistory(
            decision_id=decision.id, actor="System", stage="system", action="po_created",
            from_status="pending_final", to_status="po_ready",
            previous_qty=decision.current_qty, new_qty=decision.current_qty,
            reason=f"Purchase order {po_number} created from approved decision",
            otb_impact_value=decision.otb_impact_value, timestamp=now,
        ))

    db.commit()
    db.refresh(decision)
    return decision_to_dict(decision)
