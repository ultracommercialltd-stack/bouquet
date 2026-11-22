"""Strict JSON schemas for Puppet Master tools."""

NS_ROSTER_STATUS = {
    "name": "ns_roster_status",
    "description": "List authorized NationStates roster names and safe policy state; never returns credentials.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}

NS_NATION_STATUS = {
    "name": "ns_nation_status",
    "description": "Read current public state for one NationStates nation using combined official API shards.",
    "parameters": {
        "type": "object",
        "properties": {"nation": {"type": "string", "minLength": 1}},
        "required": ["nation"],
        "additionalProperties": False,
    },
}

NS_KEEPALIVE = {
    "name": "ns_keepalive",
    "description": "Register an API ping for enabled authorized puppets. Omit nations to process all enabled roster entries.",
    "parameters": {
        "type": "object",
        "properties": {
            "nations": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "uniqueItems": True,
            }
        },
        "additionalProperties": False,
    },
}

NS_GET_ISSUES = {
    "name": "ns_get_issues",
    "description": "Read pending issues and options for an authorized puppet, including its assigned decision profile.",
    "parameters": {
        "type": "object",
        "properties": {"nation": {"type": "string", "minLength": 1}},
        "required": ["nation"],
        "additionalProperties": False,
    },
}

NS_ANSWER_ISSUE = {
    "name": "ns_answer_issue",
    "description": "Policy-gated NationStates API issue action. Disabled unless both global and per-nation operator policy allow it.",
    "parameters": {
        "type": "object",
        "properties": {
            "nation": {"type": "string", "minLength": 1},
            "issue_id": {"type": "integer", "minimum": 1},
            "option_id": {"type": "integer", "minimum": -1},
        },
        "required": ["nation", "issue_id", "option_id"],
        "additionalProperties": False,
    },
}

NS_ACTION_POLICY = {
    "name": "ns_action_policy",
    "description": "Classify a requested NationStates operation as autonomous, policy-gated, manual-only via Bouquet, denied, or unknown.",
    "parameters": {
        "type": "object",
        "properties": {"action": {"type": "string", "minLength": 1}},
        "required": ["action"],
        "additionalProperties": False,
    },
}
