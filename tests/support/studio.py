"""Drive the production WebChannel from Qt tests."""


def evaluate(qtbot, window, script):
    results = []
    window.page.runJavaScript(script, results.append)
    qtbot.waitUntil(lambda: bool(results), timeout=5000)
    return results[0]


def edit_draft(qtbot, window, value):
    import json

    qtbot.waitUntil(lambda: window.attached)
    expression = (
        "(()=>{const editor=document.querySelector('[data-form=message] textarea');"
        f"editor.value={json.dumps(value)};"
        "editor.dispatchEvent(new Event('input',{bubbles:true}));"
        "return true;})()"
    )
    assert evaluate(qtbot, window, expression) is True
    qtbot.waitUntil(lambda: window.chat_coordinator.draft(window.identity).text == value)
