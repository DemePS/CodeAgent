"""ask_human: Claude asks the person a question mid-task."""

from .. import state


def tool_ask_human(question: str) -> str:
    state.ui.panel(question, tone="question")
    if state.auto_mode:
        state.ui.status("(autonomous mode: not waiting for an answer)")
        return (
            "Autonomous mode is on and the user is not available. Choose the most reasonable "
            "option yourself, continue, and list this assumption in your final answer."
        )
    answer = state.ui.ask_text("Your answer (multi-line paste is fine): ", multiline=True)
    return answer or "(the user gave no answer)"


