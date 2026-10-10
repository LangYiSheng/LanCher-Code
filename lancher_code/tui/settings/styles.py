"""设置界面的共享样式。"""
SETTINGS_CSS = """
    SettingsScreen { background: $background; color: $text; align-horizontal: center; }
    #settings-root { width: 100%; max-width: 104; height: 100%; padding: 1 2; }
    #settings-title { height: auto; color: $text; text-style: bold; }
    #settings-tabs { height: 2; margin-top: 1; border-bottom: solid $panel; }
    .settings-tab { width: 1fr; height: 1; min-width: 0; border: none; padding: 0; background: transparent; color: $text-muted; }
    .settings-tab.-selected { color: $text; text-style: bold; }
    .settings-tab:focus, .settings-tab.-selected:focus { background: $foreground; color: $background; text-style: bold; }
    #settings-error { color: $error; height: auto; display: none; }
    #settings-restart { color: $warning; height: auto; display: none; }
    #settings-notice { color: $success; height: auto; display: none; }
    #settings-pages { height: 1fr; margin-top: 1; }
    .settings-page { height: auto; display: none; }
    .settings-page.-active { display: block; }
    .field { height: auto; margin-bottom: 1; }
    .field-label { height: auto; color: $text-muted; }
    .form, .list-region, .row-actions { height: auto; }
    .editor-title { height: auto; text-style: bold; color: $text; margin-bottom: 1; }
    .scope-note { height: auto; color: $text-muted; margin: 1 0; }
    Input, Select { width: 100%; height: 3; border: none; border-bottom: solid $panel; background: transparent; color: $text; padding: 0 1; }
    Input:focus, Select:focus { border-bottom: solid $primary; }
    Select > SelectCurrent { background: transparent; border: none; color: $text; }
    Select > SelectOverlay { background: $surface; color: $text; border: solid $primary; }
    Checkbox { height: 3; background: transparent; color: $text; }
    Collapsible { height: auto; background: transparent; border: none; padding: 0; }
    Button { background: transparent; color: $text; text-style: none; border: none; min-width: 8; height: 2; padding: 0 1; }
    Button:hover { background: $surface; color: $text; }
    Button:focus { background: $foreground; color: $background; text-style: bold; }
    .link-button { width: auto; max-width: 100%; margin: 0 0 1 0; }
    #model-catalog .link-button { height: 1; margin: 0; color: $text-muted; }
    #model-catalog .link-button:focus { background: $foreground; color: $background; }
    .model-usage-row { height: 1; width: 100%; }
    .model-usage-row .link-button { width: 8; min-width: 8; padding: 0 1; }
    #catalog-heading { height: 1; margin-top: 1; text-style: bold; }
    #current-model-summary, #default-model-summary { height: 1; width: 1fr; }
    #settings-root.-narrow #model-catalog .link-button { margin: 0; }
    #settings-root.-narrow .catalog-help { margin: 0; }
    .danger-button { color: $text-muted; width: auto; max-width: 100%; }
    .danger-button:focus { background: $foreground; color: $background; }
    DataTable { height: 10; min-height: 4; background: transparent; color: $text; }
    DataTable > .datatable--header { color: $text; background: $surface; text-style: bold; }
    DataTable > .datatable--cursor { background: transparent; text-style: none; }
    DataTable:focus > .datatable--cursor { background: $foreground; color: $background; text-style: bold; }
    #model-tree:focus { background-tint: transparent; }
    #model-tree > .tree--cursor { background: transparent; text-style: none; }
    #model-tree:focus > .tree--cursor { background: $foreground; color: $background; text-style: none; }
    #model-tree > .tree--highlight-line { background: transparent; }
    #settings-actions { height: auto; border-top: solid $panel; padding-top: 1; }
    #settings-actions Button { margin-right: 1; }
    #settings-save { color: $primary; text-style: bold; }
    #settings-save:focus { background: $foreground; color: $background; }
    #settings-cancel { color: $text-muted; }
    #settings-cancel:focus { background: $foreground; color: $background; }
    #settings-help { height: auto; color: $text-muted; }
    #settings-root.-narrow { padding: 0 1; }
    #settings-root.-narrow #settings-tabs { margin-top: 0; }
    #settings-root.-narrow .settings-tab.-selected { text-style: bold underline; }
    #settings-root.-narrow #settings-actions { padding-top: 0; }
    #settings-root.-narrow #settings-help { display: none; }
    #settings-root.-narrow #settings-pages { margin-top: 0; }
    """
