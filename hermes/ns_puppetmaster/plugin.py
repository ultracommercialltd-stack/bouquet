"""Register guarded NationStates tools with Hermes."""

from . import schemas, tools


def register(ctx):
    registrations = [
        ("ns_roster_status", schemas.NS_ROSTER_STATUS, tools.ns_roster_status),
        ("ns_nation_status", schemas.NS_NATION_STATUS, tools.ns_nation_status),
        ("ns_keepalive", schemas.NS_KEEPALIVE, tools.ns_keepalive),
        ("ns_get_issues", schemas.NS_GET_ISSUES, tools.ns_get_issues),
        ("ns_answer_issue", schemas.NS_ANSWER_ISSUE, tools.ns_answer_issue),
        ("ns_create_onboarding_link", schemas.NS_CREATE_ONBOARDING_LINK, tools.ns_create_onboarding_link),
        ("ns_action_policy", schemas.NS_ACTION_POLICY, tools.ns_action_policy),
    ]
    for name, schema, handler in registrations:
        ctx.register_tool(
            name=name,
            toolset="ns-puppetmaster",
            schema=schema,
            handler=handler,
        )
