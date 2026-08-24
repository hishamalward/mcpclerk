# vhs examples/demo.tape  ->  examples/demo.gif
Output examples/demo.gif
Set Shell bash
Set FontSize 15
Set Width 1180
Set Height 680
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms
Set Padding 16

Hide
Type "source .venv/bin/activate && rm -rf audit approvals && clear" Enter
Sleep 800ms
Show

Type "python examples/demo_driver.py 2>/dev/null &" Enter
Sleep 7s
Type "mcpclerk approve --once" Enter
Sleep 2s
Type "y" Enter
Sleep 5s
Type "mcpclerk report audit/mcpclerk.jsonl" Enter
Sleep 5s
