"""Fictional, explicit invitation used for gate tests; not real timing evidence."""

INVITATION = (
    "I am gathering input on the evaluation protocol this week before finalizing it; "
    "please contact me now to compare methods."
)


def assessment(source_id, quote=INVITATION):
    return {
        "mechanism": "active_work_need",
        "person_impact": {
            "statement": "The person is currently revising a protocol and gathering external input.",
            "evidence_id": source_id,
            "quote": quote,
        },
        "opportunity": {
            "statement": "They explicitly invite a discussion of methods during that revision.",
            "evidence_id": source_id,
            "quote": quote,
        },
        "waiting_cost": {
            "statement": "Input after finalization would miss the stated design decision period.",
            "evidence_id": source_id,
            "quote": quote,
        },
    }


def purpose(source_id, quote=INVITATION):
    return {"contact_purpose": "rapport", "purpose_reason": {
        "statement": "The invitation supports a methods discussion, not a hiring approach.",
        "evidence_id": source_id, "quote": quote,
    }}
