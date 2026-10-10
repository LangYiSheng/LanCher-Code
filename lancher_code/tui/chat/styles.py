"""对话主界面的样式，与交互控制分离。"""
CHAT_CSS = """
    #root { height: 100%; width: 100%; max-width: 112; layout: vertical; }
    #chat-view { height: 1fr; width: 100%; }
    .message { width: 100%; height: auto; layout: vertical; }
    .message-label, .message-body { height: auto; width: 1fr; }
    #chat-view.-banner-collapsed { margin-top: 0; }
    #composer-region { width: 1fr; height: auto; layout: vertical; }
    #composer { height: 2; min-height: 2; max-height: 7; width: 1fr; }
    #prompt-glyph { width: 2; content-align: center middle; text-style: bold; }
    #composer-input { width: 1fr; height: 100%; min-height: 1; max-height: 6; margin: 0; padding: 0; background: transparent; border: none; }
    #composer-input:focus { border: none; background: transparent; }
    #composer-input .text-area--cursor-line { background: transparent; }
    #slash-command-menu { display: none; width: 1fr; height: auto; }
    .slash-command-item { padding: 0 1; width: 1fr; height: auto; }
    #command-hint { width: 1fr; }
    #exit-hint { width: 1fr; height: auto; display: none; color: $warning; padding: 0 1; }
    #inline-permission-panel { width: 1fr; }
    #permission-title, #permission-command, #permission-details, #permission-description,
    #permission-prompt, .permission-preview, .permission-option, #permission-help { height: auto; }
    #permission-title { text-style: bold; }
    .permission-option { width: 1fr; padding: 0 1; }
    #status-bar { width: 1fr; }
    #status-center, #status-right { text-align: right; }
    Screen { background: $background; color: $text; align-horizontal: center; }
    #banner { margin: 1 2 0 2; height: auto; color: $text-muted; }
    #banner.-compact { margin: 0 2; }
    #stage-bar { margin: 0 2; height: 1; width: 1fr; }
    .stage-arrow { width: 2; height: 1; color: $text-muted; content-align: center middle; }
    #phase-explanation { width: 1fr; height: 1; color: $text-muted; padding-left: 2; }
    .quiet-action { border: none; height: 1; min-height: 1; min-width: 4; width: auto; padding: 0 1; background: transparent; color: $text-muted; text-style: none; }
    .quiet-action:hover { background: $surface; color: $text; }
    .quiet-action.-selected { color: $primary; text-style: bold; }
    .quiet-action:focus { background: $foreground; color: $background; text-style: bold; }
    #chat-view { margin: 1 1 0 1; padding: 0 1; }
    .message { padding: 0; margin: 0 0 1 0; border: none; }
    .message--user, .message--assistant, .message--system, .message.-error { border: none; }
    .message--user, .message--assistant { padding: 0; }
    .message-label { color: $primary; text-style: bold; }
    .message-timeline { height: auto; width: 1fr; }
    .timeline-text { height: auto; width: 1fr; margin: 0; }
    .trace-section { height: auto; width: 1fr; margin: 0; }
    .standalone-compaction { margin-bottom: 1; }
    .timeline-separator { margin-top: 1; }
    .trace-header { height: 1; width: 1fr; color: $text-muted; }
    .trace-header:focus { text-style: bold underline; }
    .trace-body { height: auto; width: 1fr; padding-left: 2; color: $text-muted; }
    .tool-calls { height: auto; width: 1fr; padding-left: 2; }
    .tool-calls.-single { padding-left: 0; }
    .tool-call-trace { margin: 0; }
    .tool-call-body { padding-bottom: 0; }
    #composer-region { margin: 0 2; max-height: 75%; }
    #composer { border-top: solid $panel; padding: 0; }
    #composer:focus-within { border-top: solid $primary; }
    #composer-input { color: $text; }
    #composer-input .text-area--placeholder { color: $text-muted; }
    #composer-input .text-area--cursor { background: $primary; color: $background; }
    #prompt-glyph { color: $primary; }
    #composer-actions { height: 1; width: 1fr; }
    #composer-help { width: 1fr; height: 1; color: $text-muted; }
    #command-hint { margin: 0; color: $text-muted; height: auto; }
    #slash-command-menu { border: none; background: $surface; max-height: 7; margin: 0; }
    .slash-command-item.-active { background: $foreground; color: $background; }
    #approval-region { height: auto; max-height: 12; display: none; }
    #inline-permission-panel { height: auto; max-height: 12; background: $surface; border-left: solid $warning; padding: 0 1; }
    #inline-permission-panel:focus { border-left: solid $warning; }
    #permission-title { color: $warning; margin: 0; }
    #permission-command, #permission-details, #permission-description, #permission-prompt, .permission-preview { color: $text; margin: 0; }
    #permission-cwd, #permission-description { color: $text-muted; height: auto; }
    .permission-preview.-error { color: $error; }
    .permission-preview.-success { color: $success; }
    .permission-option.-active { background: $foreground; color: $background; }
    #permission-help { color: $text-muted; margin: 0; }
    #pending-queue { display: none; height: auto; max-height: 8; overflow-y: auto; background: $surface; }
    .queue-heading, .queue-actions { height: 1; }
    .queue-heading Static { width: 1fr; height: 1; }
    .queue-item { height: auto; padding: 0 1; margin-bottom: 1; }
    .queue-text { height: auto; max-height: 2; }
    #plan-panel { display: none; height: auto; max-height: 6; color: $text-muted; }
    #plan-preview { height: auto; max-height: 4; }
    .plan-actions { height: 1; }
    #plan-execute { color: $primary; text-style: bold; }
    #plan-execute:focus { color: $background; }
    #status-bar { margin: 0 2 1 2; height: 1; color: $text-muted; background: $surface; }
    #status-left { color: $text-muted; width: 1fr; }
    #status-center { width: auto; max-width: 35%; }
    #status-right { width: auto; margin-left: 1; color: $text-muted; }
    #status-details-scroll { display: none; height: auto; max-height: 8; margin: 0 2; overflow-x: hidden; }
    #status-details { height: auto; color: $text-muted; }
    Screen.-narrow #phase-explanation { display: none; }
    Screen.-narrow #status-center { display: none; }
    Screen.-narrow #status-bar { height: 2; }
    Screen.-narrow #status-left { height: 2; }
    Screen.-tiny #status-bar, Screen.-tiny #status-left { height: 3; }
    Screen.-tiny #composer-actions.-working #chat-model,
    Screen.-tiny #composer-actions.-working #chat-policy,
    Screen.-tiny #composer-actions.-working #chat-details { display: none; }
    Screen.-narrow #status-right { display: none; }
    Screen.-narrow #composer-help { display: none; }
    Screen.-narrow #banner { margin-top: 0; }
    """
