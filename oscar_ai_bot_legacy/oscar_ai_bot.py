"""
Oscar AI — the dedicated RFQ / quotation bot that answers DIRECT MESSAGES sent to
the "Oscar AI" account (see scripts/create_oscar_bot.py + the DM interception in
main.py:create_direct_message).

This is a FOCUSED agent, deliberately separate from the general Oscar assistant
(agent.run_agent): it is wired with ONLY the RFQ tool and a quotation-specialist
system prompt, so a DM to Oscar AI can check the mailbox and prepare quotations —
and nothing else (it won't create tasks/meetings/etc. by accident). The general
Oscar tab keeps the full toolset.

Pricing guardrail is inherited from the tool/pipeline: prices and part numbers come
ONLY from the company price lists — the model never invents money.
"""

import logging

from langchain_core.messages import (AIMessage, HumanMessage, SystemMessage,
                                      ToolMessage)
from langchain_openai import ChatOpenAI

from tools.rfq_tools import make_rfq_tools

logger = logging.getLogger(__name__)

_llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)

_MAX_STEPS = 4  # one tool round-trip is enough; bound the loop as a safety net.

_SYSTEM = """You are **Oscar AI**, a focused RFQ (Request For Quotation) assistant \
for the team. You are reached through a direct-message chat, and you do exactly ONE \
job: help with quotation-request emails.

WHAT YOU DO
- You are the team's super-admin RFQ assistant. When the user asks anything like \
"did we get any RFQ / quotation / enquiry emails", "check for new quotations", "any \
new RFQs", "prepare quotations for new enquiries" — call the `check_rfq_emails` tool \
and report what it returns.
- The tool reads the mailbox, prepares a quotation for each NEW RFQ, and AUTOMATICALLY \
CREATES A TASK assigned to the team lead carrying that quotation. Already-processed \
RFQs come back with their existing quotation (no new task).
- After the tool runs, reply in clear, friendly chat prose. For each RFQ include:
  • the customer / company
  • the quotation number
  • the subtotal (₹)
  • matched vs unmatched item counts
  • the editable quotation link
  • a short list of any UNMATCHED items (so a human can add them manually).
  • whether it is NEW (say "I've assigned a task to the team lead") or already \
processed (say "already quoted earlier").
- If no RFQ emails were found, say so plainly.

HARD RULES
- Prices, part numbers and totals come ONLY from the tool result (the company price \
lists). NEVER invent or guess a price, part number, or total.
- You ONLY handle RFQ / quotation email requests. If the user asks for anything else \
(creating tasks, meetings, reminders, WhatsApp, general chit-chat), politely say you \
only handle RFQ / quotation emails here, and suggest they use the main Oscar assistant \
tab for other things. Do NOT attempt those actions.
- Never expose internal ids, stack traces, or tool JSON — summarise for a human.
"""


def run_rfq_agent(user_id: int, message: str) -> str:
    """Run the focused RFQ agent for one DM turn and return the reply text."""
    tools = make_rfq_tools(user_id)
    tool_map = {t.name: t for t in tools}
    llm = _llm.bind_tools(tools)

    messages = [SystemMessage(content=_SYSTEM), HumanMessage(content=message)]
    for _ in range(_MAX_STEPS):
        ai: AIMessage = llm.invoke(messages)
        messages.append(ai)
        if not ai.tool_calls:
            return (ai.content or "").strip() or \
                "I couldn't find anything to report just now."
        for tc in ai.tool_calls:
            tool = tool_map.get(tc["name"])
            try:
                result = tool.invoke(tc["args"]) if tool else {"error": "unknown tool"}
            except Exception as e:  # never let a tool crash kill the reply
                logger.error("[OSCAR-AI] tool %s failed: %s", tc.get("name"), e,
                             exc_info=True)
                result = {"error": str(e)}
            messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))

    return ("I checked but couldn't finish preparing the reply — please try again, "
            "or use the main Oscar tab.")
