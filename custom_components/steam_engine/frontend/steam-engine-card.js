// Steam Engine dashboard card: <steam-engine-card> and its editor <steam-engine-card-editor>.
//
// One ES module without a build step and without external libraries. The integration serves it
// itself. Every action carries the app ID of the row that was shown and calls the integration's
// action (service) directly with return_response, so a list that changes in between cannot hit the
// neighbouring game. Entity IDs come from the WebSocket command steam_engine/entities (they can be
// renamed), never from fixed names; the image URLs of the games from the subscription
// steam_engine/subscribe_images where the integration resolved them (newer games have no picture at the CDN's old paths); the install
// size of a game that is not installed from steam_engine/game_size, asked once when its sheet or
// install dialog opens. The PC section uses the integration's PC switch and auto off
// switch and the agent's shut down, restart, lock and close games buttons; those services answer
// nothing, so they are called without return_response.
//
// The file has two layers:
// - The logic layer (texts, formatting, configuration, the model built from the states, which
//   buttons show, confirmations, answers, job tracking, tabs) is made of pure functions, exported
//   for the tests in tests/js (node --test), and the controller part of the custom element (hass,
//   entity IDs, busy locks, dialogs, messages, service calls).
// - The render layer turns a view (the model plus the card's own state) into HTML. It never calls
//   Home Assistant; the controller finds its controls by their data-action / data-key attributes.
//   There are two styles (see RENDERERS), chosen by the option style: "library" (the default; game
//   artwork: a grid of capsule images and a detail sheet per game) and "tiles" (Home Assistant
//   tiles and rows). Both share the logic layer.
// The custom elements are only defined where a browser provides them, so the module can be
// imported in node.

export const CARD_TYPE = 'steam-engine-card';
export const EDITOR_TYPE = 'steam-engine-card-editor';
export const DOMAIN = 'steam_engine';
// Also the default order. Free space is the first tab: it is short, so opening the card does not
// start with the long list of installed games.
export const SECTIONS = ['status', 'pc', 'free_space', 'updates', 'installed', 'history', 'not_installed'];
// Status and PC are always shown at the top (when listed in sections), the lists below as tabs
export const TOP_SECTIONS = ['status', 'pc'];
export const LIST_SECTIONS = ['free_space', 'updates', 'installed', 'history', 'not_installed'];
export const HISTORY_LIMIT = 10; // entries of the update history the card shows
export const PICTURE_URL = 'https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg';
// The small capsule image (231 x 87, about 10 KB) of the library style's grid
export const CAPSULE_URL = 'https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/capsule_231x87.jpg';
// The card's styles, the first is the default
export const STYLES = ['library', 'tiles'];
const TAB_STORAGE_KEY = 'steam-engine-card.tab';
const DETAILS_STORAGE_KEY = 'steam-engine-card.pc-details'; // '1' while the PC details are open
const REFETCH_MS = 60 * 1000; // at most one steam_engine/entities request per minute
// Waits before the next try when subscribing to the images failed (Home Assistant still starting:
// the integration not loaded yet); after the last one the card waits for a reconnect or the next
// answer of steam_engine/entities
export const IMAGES_RETRY_MS = [5000, 15000, 60000];
const UNINSTALL_WATCH_MS = 15 * 60 * 1000; // watch for the game to leave the library
const MOVE_WATCH_MS = 6 * 60 * 60 * 1000; // watch for the end of a move to another drive
const JOB_WATCH_MS = 12 * 60 * 60 * 1000; // watch for the end of an install or update job
const MESSAGE_MS = { success: 10000, info: 10000, error: 30000 };
const MAX_MESSAGES = 4;
const NOT_A_STATE = ['unknown', 'unavailable', ''];

// --- texts -----------------------------------------------------------------------------------
// Wording of the reasons as in texts.py (pushes). Keys ending in _one / _other are picked by a count.

export const TEXTS = {
  en: {
    card_name: 'Steam Engine',
    card_description: 'Steam games on your PC: running game, job, updates, installed and owned games.',
    section_status: 'Status',
    section_pc: 'PC',
    section_updates: 'Updates',
    section_installed: 'Installed',
    section_history: 'Update history',
    section_not_installed: 'Owned, not installed',
    section_free_space: 'Free space',
    pc_on: 'PC on',
    pc_starting: 'PC is starting …',
    pc_shutting_down: 'PC is shutting down …',
    pc_restarting: 'PC is restarting …',
    pc_off: 'PC off',
    state_on: 'on',
    state_off: 'off',
    agent_online: 'agent online',
    agent_offline: 'agent offline',
    pc_wake: 'Switch the PC on',
    pc_shutdown: 'Shut down the PC',
    pc_restart: 'Restart the PC',
    pc_lock: 'Lock the PC',
    action_shutdown: 'Shut down',
    action_restart: 'Restart',
    confirm_shutdown_title: 'Shut down the PC?',
    confirm_shutdown_text: 'While Steam downloads or runs a job, or a plugin needs the PC, it stays on and you get a push instead.',
    confirm_restart_title: 'Restart the PC?',
    confirm_restart_text: 'The PC restarts now.',
    warn_game: 'A game is running and will be closed.',
    warn_steam: 'Steam is downloading or running a job. The restart interrupts it; Steam resumes the download later.',
    msg_pc_wake: 'Waking the PC …',
    msg_phase_failed_starting: 'The PC did not come on.',
    msg_phase_failed_shutting_down: 'The PC did not shut down.',
    msg_phase_failed_restarting: 'The PC did not report back after the restart.',
    msg_pc_shutdown: 'The PC shuts down',
    msg_pc_restart: 'The PC restarts',
    msg_pc_lock: 'The PC is locked',
    pc_force_shutdown: 'Shut down anyway',
    force_shutdown_hint: 'does not wait for downloads, jobs, plugins or games',
    pc_close_games: 'Close the running games',
    action_force_shutdown: 'Shut down anyway',
    action_close_games: 'Close games',
    confirm_force_shutdown_title: 'Shut down the PC anyway?',
    confirm_force_shutdown_text: 'The PC shuts down at once, even while Steam downloads, a job runs, a plugin needs the PC or a game runs.',
    confirm_close_games_title: 'Close all running games?',
    confirm_close_games_title_named: 'Close {name}?',
    confirm_close_games_text: 'All games running on the PC are closed. Progress that was not saved is lost.',
    warn_force_job: 'A Steam Engine job is running and will be interrupted.',
    warn_force_download: 'Steam is downloading. The download is interrupted; Steam resumes it later.',
    warn_plugin: 'A plugin needs the PC.',
    warn_plugin_named: 'A plugin needs the PC: {names}.',
    msg_pc_force_shutdown: 'The PC shuts down',
    msg_pc_close_games: 'The running games are being closed',
    auto_off: 'Auto off when idle',
    fact_steam: 'Steam',
    fact_steam_busy: 'Steam busy',
    fact_last_input: 'Last input',
    fact_network: 'Network',
    fact_safe: 'Safe to shut down',
    fact_off_at: 'Auto off',
    fact_plugin: 'Busy by plugin',
    pc_details: 'Details',
    pc_details_label: 'Status details of the PC',
    hold: 'Keeps the PC awake: {reasons}',
    hold_plugin: 'a plugin',
    value_running: 'running',
    value_not_running: 'not running',
    value_job: 'job running',
    value_downloading: 'downloading',
    value_yes: 'yes',
    value_no: 'no',
    off_at_value: '{time} ({relative})',
    history_installed: 'installed',
    history_updated: 'updated',
    history_removed: 'uninstalled',
    history_by_ha: 'by Steam Engine',
    history_by_steam: 'by the Steam client',
    history_build: 'build {old} → {new}',
    history_unknown: 'No update history from the PC yet.',
    history_empty: 'No entries yet.',
    all_up_to_date: 'everything up to date',
    updates_count_one: '{count} update',
    updates_count_other: '{count} updates',
    now_playing: 'Now playing',
    playing_since: 'since {time}',
    job_install: 'Installing {name}',
    job_update: 'Updating {name}',
    job_starting: 'Steam job starting',
    job_waiting: 'Steam job waiting for the PC: {names}',
    job_position: 'game {position} of {count}',
    phase_download: 'downloading',
    phase_verify: 'verifying',
    phase_commit: 'committing',
    phase_prepare: 'preparing',
    queue: 'Queue: {names}',
    progress_label: 'Progress of {name}',
    agent_never: 'The PC has not reported its games yet. Is the Steam Engine agent running on the PC?',
    not_loaded: 'Steam Engine is not set up.',
    unknown_pc: 'Steam Engine is not set up for the PC "{pc}".',
    pc_needed: 'Steam Engine is set up for several PCs: choose this card\'s PC in its settings.',
    load_error: 'Steam Engine could not be read: {error}',
    loading: 'Loading …',
    auto_update: 'Automatic updates',
    auto_update_for: 'Automatic updates for {name}',
    badge_auto: 'auto',
    mute: 'Mute update pushes',
    mute_for: 'Mute update pushes for {name}',
    badge_muted: 'muted',
    badge_queued: 'queued',
    badge_new: 'new',
    badge_playing: 'playing',
    action_update: 'Update',
    action_update_for: 'Update {name}',
    action_install_all: 'Install all',
    action_check: 'Sync now',
    action_checking: 'Syncing …',
    action_launch: 'Start',
    action_launch_for: 'Start {name}',
    action_uninstall: 'Uninstall',
    action_uninstall_for: 'Uninstall {name}',
    action_install: 'Install',
    action_install_for: 'Install {name}',
    action_dismiss: 'Dismiss',
    cancel: 'Cancel',
    auto_purchases: 'Install new purchases automatically',
    last_played: 'last played {date}',
    never_played: 'never played',
    drive_unavailable: 'drive not available',
    no_api_key: 'No owned games from Steam yet. To see them, enter the Steam Web API key and the Steam ID in the Steam Engine settings; the game details of the Steam profile must be public.',
    no_games: 'No games installed.',
    nothing_missing: 'All owned games are installed.',
    free_most: '{free} free',
    free_of: '{free} free of {total}',
    free_unknown: 'No data from the PC yet.',
    free_pc_off: 'The PC is off. Free space shows when it is on.',
    free_last_known: 'Last known state, the PC is off.',
    confirm_uninstall_title: 'Uninstall {name}?',
    confirm_uninstall_text: 'The game files ({size}) will be deleted from the PC.',
    confirm_uninstall_text_nosize: 'The game files will be deleted from the PC.',
    confirm_uninstall_text_workshop: 'The game files and workshop content ({size}) will be deleted from the PC.',
    confirm_install_title: 'Install {name}?',
    confirm_install_text: 'The game will be downloaded to the PC. A first install can take many GB.',
    confirm_install_text_size: 'About {size} will be installed on the PC.',
    install_size: 'Size of the installation',
    install_to: 'Install to',
    drive_default: "Steam's default for new games",
    drive_new_library: 'new Steam library',
    drive_no_library: 'no Steam library',
    drive_usb: 'USB',
    drive_usb_title: 'USB drive',
    drive_ssd: 'SSD',
    drive_hdd: 'HDD',
    drive_fits: 'fits',
    drive_no_fit: 'does not fit ({missing} missing)',
    drive_preselected: '{full} is too full, {drive} preselected.',
    warn_no_space: 'Probably not enough space on {drive} ({missing} missing: needs {needed}, {free} free).',
    size_on_drive: '{size} on {drive}',
    on_drive: 'on {drive}',
    drive_games_one: '{count} game',
    drive_games_other: '{count} games',
    drive_games_none: 'No games on this drive.',
    confirm_pc_off: 'The PC is switched on for it.',
    msg_waking: 'Switching the PC on for {name} …',
    msg_install_sent: 'Installation of {name} started',
    msg_update_sent: 'Update of {name} started',
    msg_uninstall_sent: 'Uninstalling {name} started',
    msg_launch_sent: '{name} is starting',
    msg_queued: '{name} is in the queue',
    msg_pending: '{name}: the PC has not taken the job yet. It starts once the agent reports.',
    msg_rejected: '{name}: not done, {reason}',
    msg_error: '{name}: {error}',
    msg_uninstalled: '{name} uninstalled',
    msg_installed: '{name} installed',
    msg_updated: '{name} updated',
    msg_failed: '{name}: the job failed (see the notification)',
    msg_no_space: '{name}: not enough space',
    msg_skipped: '{name}: already running in the Steam client',
    msg_game_running: '{name}: not started, a game is running. Starts by itself when the game is closed.',
    msg_aborted: '{name}: the Steam job was interrupted. It continues by itself once the PC is on.',
    msg_check_done: 'Sync done: {result}',
    msg_check_skipped: 'No update check right now (one is running, or the PC has not reported its games).',
    new_purchases_one: '{count} new purchase',
    new_purchases_other: '{count} new purchases',
    reason_invalid_appid: 'invalid app ID',
    reason_game_running: 'a game is running',
    reason_agent_offline: 'the Steam agent on the PC cannot be reached',
    reason_already_queued: 'already in the running job or the queue',
    reason_already_sent: 'sent a moment ago already',
    reason_publish_failed: 'the command could not be sent over MQTT',
    reason_unknown: 'no answer from the PC',
    action_move: 'Move',
    confirm_move_title: 'Move {name}?',
    confirm_move_text: 'The game is copied from {drive} to the drive picked; Steam closes for a moment near the end. Nothing is downloaded again, the old folder is deleted at the end.',
    confirm_move_text_size: 'The game ({size}) is copied from {drive} to the drive picked; Steam closes for a moment near the end. Nothing is downloaded again, the old folder is deleted at the end.',
    move_to: 'Move to',
    job_move: 'Moving {name}',
    chip_moving: 'Moving {percent} %',
    tag_moving: 'moving',
    move_route: '{from} → {to}',
    move_phase_requested: 'waiting for the PC',
    move_phase_copy: 'copying',
    move_phase_sync: 'Steam closed, copying the rest',
    move_phase_switch: 'Steam closed, switching over',
    move_phase_verify: 'Steam starting with the new drive',
    move_phase_cleanup: 'deleting the old folder',
    move_phase_rollback: 'taking the move back',
    msg_move_sent: 'Moving {name} started',
    msg_moved: '{name} moved to {drive}',
    msg_moved_left: '{name} moved to {drive}; the old folder could not be deleted completely (the rest goes at the next start of the agent)',
    msg_move_failed: '{name}: not moved, {reason}',
    reason_job_running: 'another Steam job is running',
    reason_moving: 'a game is being moved',
    reason_not_installed: 'not installed',
    reason_invalid_drive: 'invalid drive',
    reason_invalid_request: 'invalid app ID or drive',
    reason_drive_unavailable: 'the drive of the game or the target drive is not available',
    reason_same_drive: 'the game is on that drive already',
    reason_steam_busy: 'Steam is working on the game right now',
    reason_drive_not_suitable: 'the drive cannot take a new Steam library',
    reason_library_not_created: 'the new Steam library could not be set up',
    reason_library_config_lost: 'Steam lost its list of libraries, the old list was put back',
    reason_library_unreadable: 'the list of Steam libraries could not be read',
    reason_unexpected_folder: 'unexpected install folder',
    reason_shared_folder: 'another app uses the same game folder',
    reason_no_space: 'not enough space on the target drive',
    reason_target_exists: 'the target drive has files of the game already',
    reason_copy_failed: 'copying failed',
    reason_steam_did_not_exit: 'the Steam client did not exit',
    reason_not_accepted: 'Steam did not take the game on the new drive',
    reason_rollback_failed: 'the move could not be taken back yet (see the notification)',
    reason_interrupted: 'the move was interrupted and taken back',
    move_reason_game_running: "a game or a program from the game's folder is running",
    reason_unexpected_error: 'an unexpected error',
    game_fallback: 'Game {appid}',
    editor_title: 'Title (empty: none)',
    editor_pc: 'PC',
    editor_show_images: 'Show game images',
    editor_actions_when_pc_off: 'Show actions while the PC is off (a tap switches it on)',
    tabs_label: 'Games',
    tab_updates: 'Updates',
    tab_installed: 'Installed',
    tab_history: 'History',
    tab_not_installed: 'Owned',
    tab_free_space: 'Space',
    btn_wake: 'Switch on',
    btn_shutdown: 'Shut down',
    btn_restart: 'Restart',
    btn_lock: 'Lock',
    btn_close_games: 'Close games',
    chip_downloading: 'Downloading {percent} %',
    chip_steam_downloading: 'Steam downloading',
    tag_downloading: 'downloading',
    tag_update: 'update',
    used_percent: '{percent} % used',
    editor_style: 'Style',
    style_tiles: 'Tiles',
    style_library: 'Library (game artwork)',
    action_close: 'Close',
    not_installed: 'not installed',
    update_size: 'update {size}',
    tag_drive_missing: 'drive missing',
    sheet_pc_off: 'Switch the PC on to start, update, install or uninstall the game.',
    badge_deferred: 'waiting',
    deferred_waiting: 'Waiting for active hours',
    deferred_at: 'starts at {time}',
    deferred_day: 'starts {day} at {time}',
    deferred_date: 'starts on {date} at {time}',
    fact_active_hours: 'Active hours',
    active_hours_all_day: 'all day',
  },
  de: {
    card_name: 'Steam Engine',
    card_description: 'Steam-Spiele auf dem PC: laufendes Spiel, Auftrag, Updates, installierte und gekaufte Spiele.',
    section_status: 'Status',
    section_pc: 'PC',
    section_updates: 'Updates',
    section_installed: 'Installiert',
    section_history: 'Update-Verlauf',
    section_not_installed: 'Gekauft, nicht installiert',
    section_free_space: 'Freier Platz',
    pc_on: 'PC an',
    pc_starting: 'PC startet …',
    pc_shutting_down: 'PC fährt herunter …',
    pc_restarting: 'PC startet neu …',
    pc_off: 'PC aus',
    state_on: 'an',
    state_off: 'aus',
    agent_online: 'Agent online',
    agent_offline: 'Agent offline',
    pc_wake: 'PC einschalten',
    pc_shutdown: 'PC herunterfahren',
    pc_restart: 'PC neu starten',
    pc_lock: 'PC sperren',
    action_shutdown: 'Herunterfahren',
    action_restart: 'Neu starten',
    confirm_shutdown_title: 'PC herunterfahren?',
    confirm_shutdown_text: 'Solange Steam lädt oder einen Auftrag ausführt oder ein Plugin den PC braucht, bleibt er an, und es kommt stattdessen eine Push-Nachricht.',
    confirm_restart_title: 'PC neu starten?',
    confirm_restart_text: 'Der PC startet jetzt neu.',
    warn_game: 'Ein Spiel läuft und wird beendet.',
    warn_steam: 'Steam lädt gerade oder führt einen Auftrag aus. Der Neustart unterbricht das; Steam setzt den Download später fort.',
    msg_pc_wake: 'PC wird eingeschaltet …',
    msg_phase_failed_starting: 'Der PC ist nicht angegangen.',
    msg_phase_failed_shutting_down: 'Der PC ist nicht heruntergefahren.',
    msg_phase_failed_restarting: 'Der PC hat sich nach dem Neustart nicht zurückgemeldet.',
    msg_pc_shutdown: 'PC wird heruntergefahren',
    msg_pc_restart: 'PC startet neu',
    msg_pc_lock: 'PC wird gesperrt',
    pc_force_shutdown: 'Trotzdem herunterfahren',
    force_shutdown_hint: 'wartet nicht auf Downloads, Aufträge, Plugins oder Spiele',
    pc_close_games: 'Laufende Spiele beenden',
    action_force_shutdown: 'Trotzdem herunterfahren',
    action_close_games: 'Spiele beenden',
    confirm_force_shutdown_title: 'PC trotzdem herunterfahren?',
    confirm_force_shutdown_text: 'Der PC fährt sofort herunter, auch wenn Steam lädt, ein Auftrag läuft, ein Plugin den PC braucht oder ein Spiel läuft.',
    confirm_close_games_title: 'Alle laufenden Spiele beenden?',
    confirm_close_games_title_named: '{name} beenden?',
    confirm_close_games_text: 'Alle Spiele, die auf dem PC laufen, werden beendet. Nicht gespeicherter Fortschritt geht verloren.',
    warn_force_job: 'Ein Auftrag von Steam Engine läuft und wird abgebrochen.',
    warn_force_download: 'Steam lädt gerade. Der Download wird unterbrochen; Steam setzt ihn später fort.',
    warn_plugin: 'Ein Plugin braucht den PC.',
    warn_plugin_named: 'Ein Plugin braucht den PC: {names}.',
    msg_pc_force_shutdown: 'PC wird heruntergefahren',
    msg_pc_close_games: 'Laufende Spiele werden beendet',
    auto_off: 'Auto-Aus bei Leerlauf',
    fact_steam: 'Steam',
    fact_steam_busy: 'Steam beschäftigt',
    fact_last_input: 'Zuletzt aktiv',
    fact_network: 'Netzwerk',
    fact_safe: 'Sicher zum Abschalten',
    fact_off_at: 'Auto-Aus',
    fact_plugin: 'Durch Plugin belegt',
    pc_details: 'Details',
    pc_details_label: 'Statusdetails des PCs',
    hold: 'Hält den PC wach: {reasons}',
    hold_plugin: 'ein Plugin',
    value_running: 'läuft',
    value_not_running: 'läuft nicht',
    value_job: 'Auftrag läuft',
    value_downloading: 'lädt',
    value_yes: 'ja',
    value_no: 'nein',
    off_at_value: '{time} ({relative})',
    history_installed: 'neu installiert',
    history_updated: 'aktualisiert',
    history_removed: 'deinstalliert',
    history_by_ha: 'über Steam Engine',
    history_by_steam: 'vom Steam-Client',
    history_build: 'Build {old} → {new}',
    history_unknown: 'Noch kein Update-Verlauf vom PC.',
    history_empty: 'Noch keine Einträge.',
    all_up_to_date: 'alles aktuell',
    updates_count_one: '{count} Update',
    updates_count_other: '{count} Updates',
    now_playing: 'Läuft gerade',
    playing_since: 'seit {time}',
    job_install: '{name} wird installiert',
    job_update: '{name} wird aktualisiert',
    job_starting: 'Steam-Auftrag startet',
    job_waiting: 'Steam-Auftrag wartet auf den PC: {names}',
    job_position: 'Spiel {position} von {count}',
    phase_download: 'lädt',
    phase_verify: 'prüft',
    phase_commit: 'übernimmt',
    phase_prepare: 'bereitet vor',
    queue: 'Warteschlange: {names}',
    progress_label: 'Fortschritt von {name}',
    agent_never: 'Der PC hat seine Spiele noch nicht gemeldet. Läuft der Steam-Engine-Agent auf dem PC?',
    not_loaded: 'Steam Engine ist nicht eingerichtet.',
    unknown_pc: 'Steam Engine ist für den PC „{pc}“ nicht eingerichtet.',
    pc_needed: 'Steam Engine ist für mehrere PCs eingerichtet: Wähle den PC dieser Karte in ihren Einstellungen.',
    load_error: 'Steam Engine konnte nicht gelesen werden: {error}',
    loading: 'Lädt …',
    auto_update: 'Automatische Updates',
    auto_update_for: 'Automatische Updates für {name}',
    badge_auto: 'auto',
    mute: 'Update-Pushes stumm',
    mute_for: 'Update-Pushes stumm für {name}',
    badge_muted: 'stumm',
    badge_queued: 'in Warteschlange',
    badge_new: 'neu',
    badge_playing: 'läuft',
    action_update: 'Update',
    action_update_for: '{name} aktualisieren',
    action_install_all: 'Alle installieren',
    action_check: 'Jetzt abgleichen',
    action_checking: 'Gleicht ab …',
    action_launch: 'Starten',
    action_launch_for: '{name} starten',
    action_uninstall: 'Deinstallieren',
    action_uninstall_for: '{name} deinstallieren',
    action_install: 'Installieren',
    action_install_for: '{name} installieren',
    action_dismiss: 'Schließen',
    cancel: 'Abbrechen',
    auto_purchases: 'Neukäufe automatisch laden',
    last_played: 'zuletzt {date}',
    never_played: 'nie gespielt',
    drive_unavailable: 'Laufwerk nicht verfügbar',
    no_api_key: 'Noch keine gekauften Spiele von Steam. Um sie zu sehen, in den Einstellungen von Steam Engine den Steam-Web-API-Schlüssel und die Steam-ID eintragen; die Spieldetails des Steam-Profils müssen öffentlich sein.',
    no_games: 'Keine Spiele installiert.',
    nothing_missing: 'Alle gekauften Spiele sind installiert.',
    free_most: '{free} frei',
    free_of: '{free} frei von {total}',
    free_unknown: 'Noch keine Daten vom PC.',
    free_pc_off: 'Der PC ist aus. Der freie Platz erscheint, sobald er an ist.',
    free_last_known: 'Letzter bekannter Stand, der PC ist aus.',
    confirm_uninstall_title: '{name} deinstallieren?',
    confirm_uninstall_text: 'Die Spieldateien ({size}) werden vom PC gelöscht.',
    confirm_uninstall_text_nosize: 'Die Spieldateien werden vom PC gelöscht.',
    confirm_uninstall_text_workshop: 'Die Spieldateien und die Workshop-Inhalte ({size}) werden vom PC gelöscht.',
    confirm_install_title: '{name} installieren?',
    confirm_install_text: 'Das Spiel wird auf den PC geladen. Eine Erstinstallation kann viele GB groß sein.',
    confirm_install_text_size: 'Etwa {size} werden auf dem PC installiert.',
    install_size: 'Größe der Installation',
    install_to: 'Installieren auf',
    drive_default: 'Steam-Standard für neue Spiele',
    drive_new_library: 'neue Steam-Bibliothek',
    drive_no_library: 'keine Steam-Bibliothek',
    drive_usb: 'USB',
    drive_usb_title: 'USB-Laufwerk',
    drive_ssd: 'SSD',
    drive_hdd: 'HDD',
    drive_fits: 'passt',
    drive_no_fit: 'passt nicht (fehlen {missing})',
    drive_preselected: '{full} ist zu voll, {drive} vorgewählt.',
    warn_no_space: 'Auf {drive} ist voraussichtlich nicht genug Platz (fehlen {missing}: braucht {needed}, frei {free}).',
    size_on_drive: '{size} auf {drive}',
    on_drive: 'auf {drive}',
    drive_games_one: '{count} Spiel',
    drive_games_other: '{count} Spiele',
    drive_games_none: 'Keine Spiele auf diesem Laufwerk.',
    confirm_pc_off: 'Der PC wird dafür eingeschaltet.',
    msg_waking: 'PC wird für {name} eingeschaltet …',
    msg_install_sent: 'Installation von {name} gestartet',
    msg_update_sent: 'Update von {name} gestartet',
    msg_uninstall_sent: 'Deinstallation von {name} gestartet',
    msg_launch_sent: '{name} wird gestartet',
    msg_queued: '{name} in der Warteschlange',
    msg_pending: '{name}: Der PC hat den Auftrag noch nicht angenommen. Er startet, sobald sich der Agent meldet.',
    msg_rejected: '{name}: nicht ausgeführt, {reason}',
    msg_error: '{name}: {error}',
    msg_uninstalled: '{name} deinstalliert',
    msg_installed: '{name} installiert',
    msg_updated: '{name} aktualisiert',
    msg_failed: '{name}: Auftrag fehlgeschlagen (siehe Benachrichtigung)',
    msg_no_space: '{name}: zu wenig Speicher',
    msg_skipped: '{name}: läuft schon im Steam-Client',
    msg_game_running: '{name}: nicht gestartet, ein Spiel läuft. Startet von selbst, sobald das Spiel beendet ist.',
    msg_aborted: '{name}: Der Steam-Auftrag wurde unterbrochen. Er wird von selbst fortgesetzt, sobald der PC an ist.',
    msg_check_done: 'Abgleich fertig: {result}',
    msg_check_skipped: 'Gerade keine Update-Suche (eine läuft schon, oder der PC hat seine Spiele noch nicht gemeldet).',
    new_purchases_one: '{count} neuer Kauf',
    new_purchases_other: '{count} neue Käufe',
    reason_invalid_appid: 'ungültige App-ID',
    reason_game_running: 'es läuft gerade ein Spiel',
    reason_agent_offline: 'der Steam-Agent auf dem PC ist nicht erreichbar',
    reason_already_queued: 'schon im laufenden Auftrag oder in der Warteschlange',
    reason_already_sent: 'wurde gerade erst gesendet',
    reason_publish_failed: 'der Befehl konnte nicht über MQTT gesendet werden',
    reason_unknown: 'keine Antwort vom PC',
    action_move: 'Verschieben',
    confirm_move_title: '{name} verschieben?',
    confirm_move_text: 'Das Spiel wird von {drive} auf das gewählte Laufwerk kopiert; Steam wird gegen Ende kurz geschlossen. Nichts wird neu geladen, der alte Ordner wird am Ende gelöscht.',
    confirm_move_text_size: 'Das Spiel ({size}) wird von {drive} auf das gewählte Laufwerk kopiert; Steam wird gegen Ende kurz geschlossen. Nichts wird neu geladen, der alte Ordner wird am Ende gelöscht.',
    move_to: 'Verschieben nach',
    job_move: '{name} wird verschoben',
    chip_moving: 'Verschiebt {percent} %',
    tag_moving: 'wird verschoben',
    move_route: '{from} → {to}',
    move_phase_requested: 'wartet auf den PC',
    move_phase_copy: 'kopieren',
    move_phase_sync: 'Steam geschlossen, Rest kopieren',
    move_phase_switch: 'Steam geschlossen, umstellen',
    move_phase_verify: 'Steam startet mit dem neuen Laufwerk',
    move_phase_cleanup: 'alten Ordner löschen',
    move_phase_rollback: 'Verschieben wird zurückgenommen',
    msg_move_sent: 'Verschieben von {name} gestartet',
    msg_moved: '{name} nach {drive} verschoben',
    msg_moved_left: '{name} nach {drive} verschoben; der alte Ordner ließ sich nicht ganz löschen (der Rest folgt beim nächsten Start des Agenten)',
    msg_move_failed: '{name}: nicht verschoben, {reason}',
    reason_job_running: 'es läuft gerade ein anderer Steam-Auftrag',
    reason_moving: 'es wird gerade ein Spiel verschoben',
    reason_not_installed: 'nicht installiert',
    reason_invalid_drive: 'ungültiges Laufwerk',
    reason_invalid_request: 'ungültige App-ID oder ungültiges Laufwerk',
    reason_drive_unavailable: 'das Laufwerk des Spiels oder das Ziellaufwerk ist nicht verfügbar',
    reason_same_drive: 'das Spiel liegt schon auf diesem Laufwerk',
    reason_steam_busy: 'Steam bearbeitet das Spiel gerade',
    reason_drive_not_suitable: 'das Laufwerk kann keine neue Steam-Bibliothek aufnehmen',
    reason_library_not_created: 'die neue Steam-Bibliothek ließ sich nicht anlegen',
    reason_library_config_lost: 'Steam hat seine Bibliotheksliste verloren, die alte wurde zurückgespielt',
    reason_library_unreadable: 'die Liste der Steam-Bibliotheken war nicht lesbar',
    reason_unexpected_folder: 'unerwarteter Installationsordner',
    reason_shared_folder: 'eine andere App nutzt denselben Spielordner',
    reason_no_space: 'zu wenig Platz auf dem Ziellaufwerk',
    reason_target_exists: 'auf dem Ziellaufwerk liegen schon Dateien des Spiels',
    reason_copy_failed: 'Kopieren fehlgeschlagen',
    reason_steam_did_not_exit: 'der Steam-Client hat sich nicht beendet',
    reason_not_accepted: 'Steam hat das Spiel auf dem neuen Laufwerk nicht übernommen',
    reason_rollback_failed: 'das Verschieben ließ sich noch nicht zurücknehmen (siehe Benachrichtigung)',
    reason_interrupted: 'das Verschieben wurde unterbrochen und zurückgenommen',
    move_reason_game_running: 'es läuft ein Spiel oder ein Programm aus dem Spielordner',
    reason_unexpected_error: 'unerwarteter Fehler',
    game_fallback: 'Spiel {appid}',
    editor_title: 'Titel (leer: keiner)',
    editor_pc: 'PC',
    editor_show_images: 'Spielbilder zeigen',
    editor_actions_when_pc_off: 'Aktionen auch bei ausgeschaltetem PC zeigen (Tippen schaltet ihn ein)',
    tabs_label: 'Spiele',
    tab_updates: 'Updates',
    tab_installed: 'Installiert',
    tab_history: 'Verlauf',
    tab_not_installed: 'Gekauft',
    tab_free_space: 'Speicher',
    btn_wake: 'Einschalten',
    btn_shutdown: 'Aus',
    btn_restart: 'Neustart',
    btn_lock: 'Sperren',
    btn_close_games: 'Spiele beenden',
    chip_downloading: 'Lädt {percent} %',
    chip_steam_downloading: 'Steam lädt',
    tag_downloading: 'lädt',
    tag_update: 'Update',
    used_percent: '{percent} % belegt',
    editor_style: 'Darstellung',
    style_tiles: 'Kacheln',
    style_library: 'Bibliothek (Spielbilder)',
    action_close: 'Schließen',
    not_installed: 'nicht installiert',
    update_size: 'Update {size}',
    tag_drive_missing: 'Laufwerk fehlt',
    sheet_pc_off: 'PC einschalten, um das Spiel zu starten, zu aktualisieren, zu installieren oder zu deinstallieren.',
    badge_deferred: 'wartet',
    deferred_waiting: 'Wartet auf Aktivitätszeit',
    deferred_at: 'startet um {time}',
    deferred_day: 'startet {day} um {time}',
    deferred_date: 'startet am {date} um {time}',
    fact_active_hours: 'Aktivitätszeit',
    active_hours_all_day: 'ganztägig',
  },
};

/** The locale of the user (hass.locale.language), canonical; 'en' when missing or invalid. */
export function localeOf(hass) {
  const tag = (hass && hass.locale && hass.locale.language) || (hass && hass.language) || 'en';
  try {
    return Intl.getCanonicalLocales(String(tag))[0] || 'en';
  } catch (e) {
    return 'en';
  }
}

// The locale for numbers of each number_format of a Home Assistant profile (as its frontend maps them)
const NUMBER_LOCALES = { comma_decimal: 'en-US', decimal_comma: 'de', space_comma: 'fr', quote_decimal: 'de-CH' };

/** The formats of the user's profile, for the formatters below: { tag (localeOf, for dates and
 * words), number (the locale for numbers, from hass.locale.number_format), grouping (false for
 * number_format 'none'), hourCycle ('h12' or 'h23' from hass.locale.time_format '12' or '24') }.
 * 'language', 'system' and anything unknown follow the language. The formatters take this or a
 * plain locale tag. */
export function formatLocaleOf(hass) {
  const tag = localeOf(hass);
  const prefs = (hass && hass.locale) || {};
  const numberFormat = String(prefs.number_format || '');
  const timeFormat = String(prefs.time_format || '');
  return {
    tag,
    number: NUMBER_LOCALES[numberFormat] || tag,
    grouping: numberFormat !== 'none',
    hourCycle: timeFormat === '12' ? 'h12' : (timeFormat === '24' ? 'h23' : undefined),
  };
}

/** A locale for the formatters (a tag or formatLocaleOf) as formatLocaleOf gives it. */
function localeParts(locale) {
  if (locale && typeof locale === 'object') return locale;
  return { tag: locale, number: locale, grouping: true, hourCycle: undefined };
}

/** The text table to use: German for every de-* locale, English otherwise. */
export function languageOf(hass) {
  return localeOf(hass).split('-')[0].toLowerCase() === 'de' ? 'de' : 'en';
}

export function t(lang, key, vars) {
  const table = TEXTS[lang] || TEXTS.en;
  let text = table[key];
  if (text === undefined) text = TEXTS.en[key];
  if (text === undefined) return key;
  if (!vars) return text;
  return text.replace(/\{(\w+)\}/g, (match, name) =>
    vars[name] === undefined || vars[name] === null ? match : String(vars[name]),
  );
}

/** Singular or plural form: key_one for exactly one, key_other otherwise. */
export function tn(lang, key, count, vars) {
  return t(lang, `${key}_${count === 1 ? 'one' : 'other'}`, { ...vars, count });
}

/** Text for a reason code of an action's answer; an unknown code is shown as it is. prefix: a text
 * of its own for some codes first (<prefix>reason_<code>, e.g. move_ for the end of a move). */
export function reasonText(lang, code, prefix = '') {
  const text = String(code || '');
  if (!text) return t(lang, 'reason_unknown');
  const key = [`${prefix}reason_${text}`, `reason_${text}`].find((k) => k in TEXTS.en);
  return key ? t(lang, key) : text;
}

// --- formatting ------------------------------------------------------------------------------

function numberFormat(locale, digits) {
  const l = localeParts(locale);
  const options = { maximumFractionDigits: digits };
  if (!l.grouping) options.useGrouping = false;
  return new Intl.NumberFormat(l.number, options);
}

/** A date and time format of the locale, with the hour cycle of the profile (formatLocaleOf). */
function dateFormat(locale, options) {
  const l = localeParts(locale);
  return new Intl.DateTimeFormat(l.tag, l.hourCycle ? { ...options, hourCycle: l.hourCycle } : options);
}

/** The relative time format of the locale ("in 5 minutes", "tomorrow"). */
function relativeFormat(locale) {
  return new Intl.RelativeTimeFormat(localeParts(locale).tag, { numeric: 'auto' });
}

/** A size in MB as the locale writes it: MB below 1 GB, otherwise GB with one decimal. '' when unknown. */
export function formatSize(mb, locale) {
  const value = Number(mb);
  if (!Number.isFinite(value) || value <= 0) return '';
  if (value < 1024) return `${numberFormat(locale, 0).format(value)} MB`;
  return `${numberFormat(locale, 1).format(value / 1024)} GB`;
}

/** The answer of steam_engine/game_size as the card keeps it: { size_mb, download_mb }, each a
 * positive number or null (unknown). */
export function gameSizeEntry(result) {
  const mb = (value) => {
    const n = Number(value);
    return value !== null && value !== undefined && Number.isFinite(n) && n > 0 ? n : null;
  };
  const r = result && typeof result === 'object' ? result : {};
  return { size_mb: mb(r.size_mb), download_mb: mb(r.download_mb) };
}

/** The install size right of a game's name: an ellipsis while it is asked (entry
 * { loading: true }), the size, '' when it is unknown or was not asked. */
export function gameSizeText(entry, locale) {
  if (!entry) return '';
  if (entry.loading) return '\u2026';
  return formatSize(entry.size_mb, locale);
}

/** How long an unknown install size (an error or no size from the integration) is kept before a
 * game opened again asks once more; the integration itself waits 10 minutes after an error. */
export const GAME_SIZE_RETRY_MS = 10 * 60 * 1000;

/** Whether the install size of a game is to be asked (again): not asked yet, or unknown since
 * GAME_SIZE_RETRY_MS (entry.at, the time of the answer). A size, or a request running, is kept. */
export function needsGameSize(entry, now) {
  if (!entry) return true;
  if (entry.loading || entry.size_mb > 0) return false;
  return !(now - Number(entry.at) < GAME_SIZE_RETRY_MS);
}

/** The known install size in MB of a game (an entry of gameSizeEntry), 0 while asked or unknown. */
export function knownGameSize(entry) {
  return entry && !entry.loading && entry.size_mb > 0 ? entry.size_mb : 0;
}

/** A size in GB (free space of the libraries). */
export function formatGb(gb, locale) {
  const value = Number(gb);
  if (!Number.isFinite(value) || value < 0) return '';
  return `${numberFormat(locale, value >= 100 ? 0 : 1).format(value)} GB`;
}

/** YYYY-MM-DD (last_played of the library) as a date of the locale; '' when empty or invalid. */
export function formatDate(isoDate, locale) {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(isoDate || ''));
  if (!match) return '';
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  if (Number.isNaN(date.getTime())) return '';
  return dateFormat(locale, { dateStyle: 'medium' }).format(date);
}

/** A point in time: the time alone when it is today, otherwise date and time. */
export function formatSince(iso, locale, now = new Date()) {
  const date = new Date(String(iso || ''));
  if (!iso || Number.isNaN(date.getTime())) return '';
  const today = date.toDateString() === now.toDateString();
  const options = today ? { timeStyle: 'short' } : { dateStyle: 'short', timeStyle: 'short' };
  return dateFormat(locale, options).format(date);
}

/** A point in time relative to now ("5 minutes ago", "in 12 minutes", "now"); '' when invalid. */
export function formatRelative(iso, locale, now = new Date()) {
  const date = new Date(String(iso || ''));
  if (!iso || Number.isNaN(date.getTime())) return '';
  const seconds = (date.getTime() - now.getTime()) / 1000;
  const abs = Math.abs(seconds);
  const format = relativeFormat(locale);
  if (abs < 60) return format.format(0, 'second');
  if (abs < 3600) return format.format(Math.round(seconds / 60), 'minute');
  if (abs < 86400) return format.format(Math.round(seconds / 3600), 'hour');
  return format.format(Math.round(seconds / 86400), 'day');
}

/** A time of day "HH:MM" (as the active hours carry it) as the locale writes it; '' when it is not one.
 * Only the clock is formatted, no time zone is applied: the window is in the time of Home Assistant. */
export function formatClock(hhmm, locale) {
  const match = /^(\d{1,2}):(\d{2})$/.exec(String(hhmm || ''));
  if (!match || Number(match[1]) > 23 || Number(match[2]) > 59) return '';
  const date = new Date(2000, 0, 1, Number(match[1]), Number(match[2]));
  return dateFormat(locale, { timeStyle: 'short' }).format(date);
}

/** The active hours { start, end } as one text: "08:00 – 20:00" (a window across midnight such as
 * "22:00 – 06:00" reads the same), "all day" when start equals end; '' without active hours. */
export function formatWindow(lang, hours, locale) {
  if (!hours || !hours.start || !hours.end) return '';
  if (hours.start === hours.end) return t(lang, 'active_hours_all_day');
  return `${formatClock(hours.start, locale)} – ${formatClock(hours.end, locale)}`;
}

/** A usable IANA time zone name (Home Assistant's hass.config.time_zone), or undefined for the
 * browser's zone when it is missing or unknown to the browser. */
function zoneOption(timeZone) {
  if (!timeZone) return undefined;
  try {
    new Intl.DateTimeFormat('en-US', { timeZone });
    return String(timeZone);
  } catch (err) {
    return undefined;
  }
}

/** The calendar day of a moment in a time zone (browser's zone without one) as a day number
 * (days since 1970-01-01) and the seconds of that day on the wall clock. */
function zoneDay(date, timeZone) {
  const zone = zoneOption(timeZone);
  if (!zone) {
    return {
      day: Math.round(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()) / 86400000),
      seconds: date.getHours() * 3600 + date.getMinutes() * 60 + date.getSeconds(),
    };
  }
  const parts = {};
  new Intl.DateTimeFormat('en-US', {
    timeZone: zone, hourCycle: 'h23', year: 'numeric', month: 'numeric', day: 'numeric',
    hour: 'numeric', minute: 'numeric', second: 'numeric',
  }).formatToParts(date).forEach((p) => { parts[p.type] = Number(p.value); });
  return {
    day: Math.round(Date.UTC(parts.year, parts.month - 1, parts.day) / 86400000),
    seconds: (parts.hour % 24) * 3600 + parts.minute * 60 + parts.second,
  };
}

/** When deferred automatic work starts: "starts at 08:00" today, "starts tomorrow at 08:00" (the
 * locale's word for tomorrow), otherwise with the date ("starts on Sun, Nov 8 at 08:00"). Shown in
 * timeZone (Home Assistant's, the zone of the active hours, so both read alike; the browser's
 * without one). Calendar days are counted there, so a day of 23 or 25 hours (DST) counts as one.
 * '' when invalid. */
export function formatDeferredStart(lang, iso, locale, now = new Date(), timeZone = '') {
  const date = new Date(String(iso || ''));
  if (!iso || Number.isNaN(date.getTime())) return '';
  const zone = zoneOption(timeZone);
  const time = dateFormat(locale, { timeStyle: 'short', timeZone: zone }).format(date);
  const days = zoneDay(date, zone).day - zoneDay(now, zone).day;
  if (days === 0) return t(lang, 'deferred_at', { time });
  if (days === 1) {
    const day = relativeFormat(locale).format(1, 'day');
    return t(lang, 'deferred_day', { day, time });
  }
  const dateText = dateFormat(locale, { weekday: 'short', day: 'numeric', month: 'short', timeZone: zone }).format(date);
  return t(lang, 'deferred_date', { date: dateText, time });
}

/** Milliseconds until just after the next midnight in timeZone (the browser's without one): a hint
 * that says "tomorrow" is rendered again then. On a day of 23 or 25 hours (DST) the first guess is
 * an hour off; it is corrected by the wall clock it lands on. */
export function msToNextDay(now = new Date(), timeZone = '') {
  const zone = zoneOption(timeZone);
  const today = zoneDay(now, zone);
  let ms = (86400 - today.seconds + 1) * 1000 - now.getMilliseconds();
  const landed = zoneDay(new Date(now.getTime() + ms), zone);
  if (landed.day > today.day) ms -= (landed.seconds - 1) * 1000;
  else ms += (86400 - landed.seconds + 1) * 1000;
  return Math.max(1000, ms);
}

const RATE_UNITS = ['B/s', 'kB/s', 'MB/s', 'GB/s'];

/** A data rate. In B/s (the agent's unit) it is scaled to kB/s, MB/s or GB/s; a unit the user chose
 * in Home Assistant is kept as it is. '' when the value is not a number. */
export function formatRate(value, unit, locale) {
  const n = Number(value);
  if (value === null || value === undefined || value === '' || !Number.isFinite(n)) return '';
  const given = String(unit || '');
  if (given && given !== 'B/s') return `${numberFormat(locale, Math.abs(n) >= 100 ? 0 : 1).format(n)} ${given}`;
  let scaled = n;
  let index = 0;
  while (Math.abs(scaled) >= 1000 && index < RATE_UNITS.length - 1) {
    scaled /= 1000;
    index += 1;
  }
  const digits = index === 0 || Math.abs(scaled) >= 100 ? 0 : 1;
  return `${numberFormat(locale, digits).format(scaled)} ${RATE_UNITS[index]}`;
}

export function pictureUrl(appid) {
  return PICTURE_URL.replace('{appid}', encodeURIComponent(String(appid)));
}

export function capsuleUrl(appid) {
  return CAPSULE_URL.replace('{appid}', encodeURIComponent(String(appid)));
}

// An image URL the integration resolved: https, or a path on Home Assistant itself (not "//host")
const IMAGE_URL = /^(https:\/\/|\/(?!\/))\S+$/;

/** The image URLs the integration resolved (the event messages of the WebSocket subscription
 * steam_engine/subscribe_images, or the answer of steam_engine/images): a map app ID
 * -> { header, capsule }, each URL or ''. Newer games have no picture at the CDN's old paths (Steam
 * answers those with a grey placeholder), only at paths with a hash that the store tells. Entries
 * that are not valid are left out. */
export function normalizeImages(result) {
  const raw = result && typeof result === 'object' ? result.images : null;
  const images = {};
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return images;
  for (const [key, entry] of Object.entries(raw)) {
    const appid = String(key).trim();
    if (!validAppid(appid) || !entry || typeof entry !== 'object') continue;
    const url = (value) => (typeof value === 'string' && IMAGE_URL.test(value.trim()) ? value.trim() : '');
    const header = url(entry.header);
    const capsule = url(entry.capsule);
    if (header || capsule) images[appid] = { header, capsule };
  }
  return images;
}

/** A text that changes exactly when the resolved images change, to render only then. */
export function imagesSignature(images) {
  return Object.keys(images || {})
    .sort()
    .map((appid) => `${appid} ${images[appid].header} ${images[appid].capsule}`)
    .join('\n');
}

/** The URL of a game's image: kind 'header' (460 x 215: hero, sheet, confirmations) or 'capsule'
 * (231 x 87: the grid). The resolved one when there is one (the other size of it when only that
 * one is resolved), otherwise fallback or the CDN's old path. */
export function steamImageUrl(images, appid, kind = 'header', fallback = '') {
  const id = String(appid === undefined || appid === null ? '' : appid).trim();
  const entry = images && id && Object.prototype.hasOwnProperty.call(images, id) ? images[id] : null;
  if (entry) {
    const url = kind === 'capsule' ? entry.capsule || entry.header : entry.header || entry.capsule;
    if (url) return url;
  }
  if (fallback) return fallback;
  return kind === 'capsule' ? capsuleUrl(id) : pictureUrl(id);
}

/** The wait in ms before the next try to subscribe to the images, attempt being the number of
 * failed tries since the last success; 0 when the card stops trying until a reconnect. */
export function imagesRetryDelay(attempt) {
  return Number.isInteger(attempt) && attempt >= 0 && attempt < IMAGES_RETRY_MS.length ? IMAGES_RETRY_MS[attempt] : 0;
}

/** True when the card should subscribe to the images again now: the integration's entities just
 * appeared in hass.states (they were missing before), so it was loaded, and no subscription is
 * running or being made. This ends the wait after a restart of Home Assistant whose integrations
 * load after the tries of IMAGES_RETRY_MS ran out. */
export function imagesResubscribeDue(wasMissing, missing, subscribed) {
  return wasMissing === true && missing === false && !subscribed;
}

// The core entities the card checks in hass.states to notice a renamed entity (see missingEntities)
const CORE_ENTITY_KEYS = ['pc', 'updates', 'not_installed', 'download_progress', 'current_game'];

/** The core entities of steam_engine/entities (its entities) that are not in hass.states, each as
 * 'key=entity_id' (the entity_id '' when the answer names none, e.g. for a disabled entity). */
export function missingEntities(entities, states) {
  const own = entities || {};
  const present = states || {};
  return CORE_ENTITY_KEYS.filter((key) => !own[key] || !present[own[key]]).map((key) => `${key}=${own[key] || ''}`);
}

/** Splits the missing entities (missingEntities) by known, those that were missing already right
 * after the last answer of steam_engine/entities (a disabled entity: asking again gives the same
 * answer): fresh are the others, a reason to ask again (an entity renamed, or the integration not
 * loaded yet); known keeps only the entries that are still missing, so an entity that comes back
 * and goes missing later counts as fresh again. */
export function splitMissing(missing, known) {
  const before = Array.isArray(known) ? known : [];
  return {
    fresh: missing.filter((entry) => !before.includes(entry)),
    known: before.filter((entry) => missing.includes(entry)),
  };
}

/** A hue (0-359) from the app ID, for the tinted placeholder of a game without a picture: the same
 * game always gets the same colour, neighbours in the grid usually differ. */
export function placeholderHue(appid) {
  let hash = 0;
  for (const ch of String(appid || '')) hash = (hash * 31 + ch.charCodeAt(0)) % 3600;
  return (hash * 7) % 360;
}

export function escapeHtml(value) {
  return String(value === undefined || value === null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// --- configuration ---------------------------------------------------------------------------

function sectionList(value, fallback, option) {
  if (value === undefined || value === null) return [...fallback];
  if (!Array.isArray(value) || value.some((s) => !SECTIONS.includes(s))) {
    throw new Error(`${option}: a list of ${SECTIONS.join(', ')}`);
  }
  return [...new Set(value)];
}

/** The style of the card: one of STYLES, the default (STYLES[0]) for anything else (a typo or a style of a newer
 * version shows the card instead of an error). */
export function normalizeStyle(value) {
  return STYLES.includes(value) ? value : STYLES[0];
}

/** The card's configuration with defaults; throws on a wrong one (Home Assistant shows the error). */
export function normalizeConfig(config) {
  if (!config || typeof config !== 'object' || Array.isArray(config)) {
    throw new Error('Invalid configuration');
  }
  return {
    ...config,
    title: config.title === undefined || config.title === null ? '' : String(config.title),
    style: normalizeStyle(config.style),
    sections: sectionList(config.sections, SECTIONS, 'sections'),
    show_images: config.show_images !== false,
    actions_when_pc_off: config.actions_when_pc_off !== false,
    pc: config.pc === undefined || config.pc === null ? '' : String(config.pc).trim(),
  };
}

/** The name of the PC without one, as the actions and the card's pc option take it. */
export const UNNAMED_PC = 'Steam PC';

/** The field "pc" for the integration's WebSocket commands and actions: a card shows one PC (its
 * option pc; without it the only PC, else the PC without a name). */
export function pcField(config) {
  return config && config.pc ? { pc: config.pc } : {};
}

/** The PC names of the answer of steam_engine/pcs ("" for the PC without a name). */
export function editorPcs(result) {
  const pcs = result && Array.isArray(result.pcs) ? result.pcs : [];
  return pcs.map((p) => (p && typeof p.pc === 'string' ? p.pc : '')).filter((p, i, all) => all.indexOf(p) === i);
}

/** The text when steam_engine/entities was refused. */
export function loadErrorText(lang, err, config) {
  const code = err && err.code;
  if (code === 'not_loaded' || code === 'pc_needed') return t(lang, code);
  if (code === 'unknown_pc') return t(lang, 'unknown_pc', { pc: (config && config.pc) || '' });
  return t(lang, 'load_error', { error: errorText(err) });
}

/** The sections shown at the top (status, PC), in the order of config.sections. */
export function topSections(config) {
  return config.sections.filter((s) => TOP_SECTIONS.includes(s));
}

/** The list sections shown as tabs, in the order of config.sections. Updates only while there are
 * any: its list and Install all have nothing to show otherwise, and Check for updates is then in
 * the status area (see checkInStatus). */
export function cardTabs(config, m) {
  const updates = !!m && Array.isArray(m.updates) && m.updates.length > 0;
  return config.sections.filter((s) => LIST_SECTIONS.includes(s) && (s !== 'updates' || updates));
}

/** The selected tab: the one remembered in this browser while it is shown, otherwise the first. */
export function selectedTab(tabs, stored) {
  return tabs.includes(stored) ? stored : (tabs[0] || '');
}

/** The tab a key moves to in the tab list (arrow keys wrap around, Home and End); '' for other keys. */
export function nextTab(tabs, current, key) {
  if (!tabs.length) return '';
  const index = Math.max(0, tabs.indexOf(current));
  switch (key) {
    case 'ArrowRight':
    case 'ArrowDown':
      return tabs[(index + 1) % tabs.length];
    case 'ArrowLeft':
    case 'ArrowUp':
      return tabs[(index - 1 + tabs.length) % tabs.length];
    case 'Home':
      return tabs[0];
    case 'End':
      return tabs[tabs.length - 1];
    default:
      return '';
  }
}

/** The number a tab shows next to its name ('' for none): updates, installed and owned games. */
export function tabCount(tab, m) {
  if (!m) return '';
  if (tab === 'updates') {
    const counted = countedUpdates(m).length;
    return counted ? String(counted) : '';
  }
  if (tab === 'installed') return m.library ? String(m.library.length) : '';
  if (tab === 'not_installed') return m.notInstalled.known ? String(m.notInstalled.games.length) : '';
  return '';
}

/** Check for updates is in the updates tab while that one is shown, otherwise in the status area. */
export function checkInStatus(config, m) {
  return !cardTabs(config, m).includes('updates');
}

// --- data from the states --------------------------------------------------------------------

const collator = new Intl.Collator(undefined, { sensitivity: 'base', numeric: true });

function byName(a, b) {
  return collator.compare(a.name || a.appid, b.name || b.appid) || collator.compare(a.appid, b.appid);
}

function validAppid(value) {
  return /^\d+$/.test(String(value === undefined || value === null ? '' : value).trim());
}

function asIds(value) {
  if (Array.isArray(value)) return value.map((i) => String(i));
  return value === undefined || value === null || value === '' ? [] : [String(value)];
}

function attrs(state) {
  return (state && state.attributes) || {};
}

function hasState(state) {
  return !!state && !NOT_A_STATE.includes(String(state.state));
}

/** A drive letter as the PC sends it ("F:", "f"), written "F:"; '' when there is none (an older
 * PC sends no drive per game). */
export function driveOf(value) {
  const text = typeof value === 'string' ? value.trim() : '';
  return /^[A-Za-z]:?$/.test(text) ? `${text[0].toUpperCase()}:` : '';
}

/** Installed games from the agent's library sensor, sorted by name; null while the PC never reported.
 * workshop_mb: the size of the game's workshop content (0 without, or from an older PC); a move
 * copies it along. */
export function libraryGames(state) {
  const games = attrs(state).games;
  if (!Array.isArray(games)) return null;
  return games
    .filter((g) => g && typeof g === 'object' && validAppid(g.appid))
    .map((g) => ({
      appid: String(g.appid).trim(),
      name: String(g.name || ''),
      size_mb: Number(g.size_mb) || 0,
      workshop_mb: Math.max(0, Number(g.workshop_mb) || 0),
      last_played: typeof g.last_played === 'string' ? g.last_played : '',
      unavailable: g.unavailable === true,
      drive: driveOf(g.drive),
    }))
    .sort(byName);
}

/** App IDs with automatic updates (attribute auto_update of the updates sensor). */
export function autoUpdateSet(state) {
  return new Set(asIds(attrs(state).auto_update));
}

/** App IDs whose updates are muted (attribute muted of the updates sensor): no update push, not
 * counted. */
export function mutedSet(state) {
  return new Set(asIds(attrs(state).muted));
}

/** Games with an update, sorted by name with the muted ones last, each marked when its updates run
 * automatically and when they are muted. */
export function updateGames(state) {
  const games = attrs(state).games;
  const auto = autoUpdateSet(state);
  const muted = mutedSet(state);
  if (!Array.isArray(games)) return [];
  return games
    .filter((g) => g && typeof g === 'object' && validAppid(g.appid))
    .map((g) => {
      const appid = String(g.appid).trim();
      return { appid, name: String(g.name || ''), size_mb: Number(g.size_mb) || 0, auto: auto.has(appid), muted: muted.has(appid) };
    })
    .sort((a, b) => (a.muted === b.muted ? byName(a, b) : a.muted ? 1 : -1));
}

/** The updates that count (the number of the tab and the chip, Install all): those not muted. */
export function countedUpdates(m) {
  return ((m && m.updates) || []).filter((g) => !g.muted);
}

/** Owned games that are not installed. known is false while there are no owned games from Steam
 * (no Steam Web API key or Steam ID, no answer yet, a private profile): then the card shows a hint
 * instead of an empty list. */
export function notInstalledGames(state) {
  const a = attrs(state);
  const raw = Array.isArray(a.games) ? a.games : [];
  const owned = Array.isArray(a.owned) ? a.owned : [];
  const fresh = new Set(asIds(a.new));
  const games = raw
    .filter((g) => g && typeof g === 'object' && validAppid(g.appid))
    .map((g) => {
      const appid = String(g.appid).trim();
      return { appid, name: String(g.name || ''), new: fresh.has(appid) };
    })
    .sort(byName);
  return { known: owned.length > 0 || games.length > 0, games };
}

/** The running game from the current game sensor, or null. */
export function currentGame(state) {
  if (!hasState(state) || !validAppid(attrs(state).appid)) return null;
  const a = attrs(state);
  return {
    appid: String(a.appid).trim(),
    name: String(state.state),
    started: a.started ? String(a.started) : '',
    picture: a.entity_picture ? String(a.entity_picture) : pictureUrl(String(a.appid).trim()),
  };
}

/** A job runs (as protocol.job_active in the integration). */
export function jobActive(state, attributes) {
  if (state === 'started' || state === 'downloading' || state === 'installed') return true;
  const position = (attributes || {}).position;
  return (state === 'skipped' || state === 'error') && position !== undefined && position !== null;
}

function percentOf(value) {
  const n = Number(value);
  return Number.isFinite(n) ? Math.max(0, Math.min(100, Math.round(n))) : 0;
}

/** The Steam job: the games handed to the PC (job), the queue and the state of the running one.
 * waiting: app IDs of automatic work waiting for the active hours (deferredWork); they are left out
 * of the queue (the card shows them as waiting), never out of the games handed to the PC. */
export function jobInfo(progress, status, waiting) {
  const pa = attrs(progress);
  const sa = attrs(status);
  const job = asIds(pa.job);
  const deferred = waiting instanceof Set ? waiting : new Set();
  const queue = asIds(pa.queue).filter((id) => !deferred.has(id) || job.includes(id));
  const active = status ? jobActive(status.state, sa) : false;
  const appid = active && validAppid(sa.appid) ? String(sa.appid).trim() : '';
  return {
    job,
    queue,
    active,
    appid,
    percent: percentOf(progress && progress.state),
    phase: active && sa.phase ? String(sa.phase) : '',
    phasePercent: percentOf(sa.phase_percent),
    position: Number(sa.position) || 0,
    count: Number(sa.count) || 0,
    show: active || job.length > 0 || queue.length > 0,
  };
}

/** The move of a game to another drive that runs, or was sent and has no answer yet (attribute move
 * of the download progress sensor, steam_move.py): { appid, name ('' before the PC named it), from
 * and drive ("F:", '' unknown), phase ('requested' until the PC's first message, then copy, sync,
 * switch, verify, cleanup or rollback), percent (0-100) }; null while none runs. */
export function moveInfo(progress) {
  const a = attrs(progress).move;
  if (!a || typeof a !== 'object' || !validAppid(a.appid)) return null;
  return {
    appid: String(a.appid).trim(),
    name: String(a.name || ''),
    from: driveOf(a.from),
    drive: driveOf(a.drive),
    phase: String(a.phase || ''),
    percent: percentOf(a.percent),
  };
}

/** The last end of a move (attribute move_result): { appid, name, from, drive, ok, reason (the
 * reason_code of a failure, '' else), warning ('source_not_deleted' or ''), updated }; null
 * without one. */
export function moveResultOf(progress) {
  const a = attrs(progress).move_result;
  if (!a || typeof a !== 'object' || !validAppid(a.appid)) return null;
  return {
    appid: String(a.appid).trim(),
    name: String(a.name || ''),
    from: driveOf(a.from),
    drive: driveOf(a.drive),
    ok: a.result === 'ok',
    reason: a.result === 'ok' ? '' : String(a.reason_code || ''),
    warning: a.warning ? String(a.warning) : '',
    updated: String(a.updated || ''),
  };
}

/** A fingerprint of the last move end, taken when a move is sent: an end equal to it is older. */
export function moveMark(result) {
  return result ? `${result.appid}|${result.ok}|${result.reason}|${result.updated}` : '';
}

/** The phase of a running move and where it goes, as texts ("F: → E:", "copying"). */
export function moveDetails(lang, move) {
  const details = [];
  if (move.from && move.drive) details.push(t(lang, 'move_route', { from: move.from, to: move.drive }));
  if (move.phase && `move_phase_${move.phase}` in TEXTS.en) details.push(t(lang, `move_phase_${move.phase}`));
  return details;
}

const CLOCK = /^([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?$/;

/** A time of day as "HH:MM" ('' when it is not one); seconds are dropped. */
function clockOf(value) {
  const match = CLOCK.exec(String(value === undefined || value === null ? '' : value).trim());
  return match ? `${match[1].padStart(2, '0')}:${match[2]}` : '';
}

/** The active hours of the updates sensor (attribute active_hours): { start, end } as "HH:MM", or
 * null while the option is off (null or missing) or the value is not valid. start equal to end is
 * the whole day, an end before the start crosses midnight. */
export function activeHours(state) {
  const value = attrs(state).active_hours;
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const start = clockOf(value.start);
  const end = clockOf(value.end);
  return start && end ? { start, end } : null;
}

/** Automatic work waiting for the active hours (attributes deferred_until and deferred of the
 * updates sensor): until is the next start of the window as ISO text ('' when nothing waits or it is
 * not a valid time), appids the waiting games (updates and new purchases). Work the user started by
 * hand never waits, so it is never in here. */
export function deferredWork(state) {
  const a = attrs(state);
  const raw = typeof a.deferred_until === 'string' ? a.deferred_until.trim() : '';
  const until = raw && !Number.isNaN(new Date(raw).getTime()) ? raw : '';
  return { until, appids: new Set(asIds(a.deferred).map((id) => id.trim()).filter(validAppid)) };
}

const DRIVE_MEDIA = new Set(['ssd', 'hdd']);

/** Icons of the card itself, for what Material Design Icons has none of: name -> path (24x24,
 * drawn with fill-rule evenodd, in the style of mdi:harddisk). */
const OWN_ICONS = {
  // An SSD: case with connector pins, screws and an "SSD" label
  'se:ssd': 'M6,2H18A2,2 0 0,1 20,4V20A2,2 0 0,1 18,22H6A2,2 0 0,1 4,20V4A2,2 0 0,1 6,2Z'
    + ' M8.6,3.6H9.8V5.6H8.6Z M10.8,3.6H12V5.6H10.8Z M13,3.6H14.2V5.6H13Z M15.2,3.6H16.4V5.6H15.2Z'
    + ' M7,18.6A0.9,0.9 0 1,0 7,20.4A0.9,0.9 0 1,0 7,18.6Z M17,18.6A0.9,0.9 0 1,0 17,20.4A0.9,0.9 0 1,0 17,18.6Z'
    + ' M7,3.7A0.9,0.9 0 1,0 7,5.5A0.9,0.9 0 1,0 7,3.7Z M5.8,8.2H18.2V16.8H5.8Z'
    + ' M6.7,10H9.7V11H7.7V12H9.7V15H6.7V14H8.7V13H6.7Z M10.5,10H13.5V11H11.5V12H13.5V15H10.5V14H12.5V13H10.5Z'
    + ' M14.3,10H16.5L17.3,10.8V14.2L16.5,15H14.3Z M15.3,11H16.3V14H15.3Z',
};

/** The icon of a drive by its media: an SSD the card's own SSD icon (Material Design Icons has
 * none), an HDD or a drive of unknown kind the hard disk. */
export function driveIcon(media) {
  return media === 'ssd' ? 'se:ssd' : 'mdi:harddisk';
}

/** Free space: the most free space of the drives with a Steam library, and each of those drives
 * from the attribute drives: { label ("F:"), letter ("f", '' when the entry has no drive letter),
 * path (its library), free_gb, total_gb, default (Steam's default library for new games, the star
 * in Steam), bus (how the disk is attached, lower case: 'usb', 'nvme', 'sata', ...; '' unknown),
 * media ('ssd' | 'hdd' | '' unknown) }. noLibrary: the drives without a library (the tab Space
 * shows them muted, the install dialog offers them for a first install: the PC creates a Steam
 * library there), same shape with path '' and default false; only those with a drive letter. null
 * without data. */
export function freeSpace(state) {
  if (!hasState(state)) return null;
  const raw = Array.isArray(attrs(state).drives) ? attrs(state).drives : [];
  const entry = (d) => {
    const drive = String(d.drive || '').trim();
    return {
      label: drive || String(d.library || ''),
      letter: /^[A-Za-z]:?$/.test(drive) ? drive[0].toLowerCase() : '',
      path: d.library ? String(d.library) : '',
      free_gb: Number(d.free_gb),
      total_gb: Number(d.total_gb),
      default: !!d.library && d.default === true,
      bus: typeof d.bus === 'string' ? d.bus.trim().toLowerCase() : '',
      media: DRIVE_MEDIA.has(String(d.media || '').toLowerCase()) ? String(d.media).toLowerCase() : '',
    };
  };
  const drives = raw.filter((d) => d && typeof d === 'object');
  return {
    free: Number(state.state),
    drives: drives.filter((d) => d.library).map(entry),
    noLibrary: drives.filter((d) => !d.library).map(entry).filter((d) => d.letter),
  };
}

/** The used space of an installed game with its drive, e.g. "31 GB on F:"; the size alone while
 * the drive is unknown (an older PC), the drive alone without a size. */
export function sizeOnDrive(lang, locale, game) {
  const size = formatSize(game && game.size_mb, locale);
  const drive = game ? driveOf(game.drive) : '';
  if (!drive) return size;
  return size ? t(lang, 'size_on_drive', { size, drive }) : t(lang, 'on_drive', { drive });
}

/** The installed games per drive letter (lower case, as freeSpace's letter), each list sorted by
 * size, the largest first. null while the library is unknown or no game names its drive (an older
 * PC): then the tab Space lists no games. */
export function gamesByDrive(library) {
  if (!Array.isArray(library) || !library.some((g) => g && driveOf(g.drive))) return null;
  const map = new Map();
  for (const game of library) {
    const drive = driveOf(game.drive);
    if (!drive) continue;
    const letter = drive[0].toLowerCase();
    if (!map.has(letter)) map.set(letter, []);
    map.get(letter).push(game);
  }
  for (const games of map.values()) games.sort((a, b) => (Number(b.size_mb) || 0) - (Number(a.size_mb) || 0) || byName(a, b));
  return map;
}

/** The rows of the tab Space, one per drive in the order the PC sends them: the drives with a
 * library, then those without (muted, with the hint note): { label, path, text (free, of total),
 * hasTotal, used (percent), default (shown with a star), library (false: no Steam library on it),
 * note, usb (a USB drive, shown with a badge), icon (by its media), letter (lower case, '' without
 * one), games (the installed games on a drive with a library, largest first; null when the PC does
 * not say where its games are, and on a drive without a library) }. */
export function driveRows(lang, locale, free, installed) {
  const drives = free && Array.isArray(free.drives) ? free.drives : [];
  const others = free && Array.isArray(free.noLibrary) ? free.noLibrary : [];
  const perDrive = gamesByDrive(installed);
  const row = (d, library) => {
    const hasTotal = Number.isFinite(d.total_gb) && d.total_gb > 0 && Number.isFinite(d.free_gb);
    const used = hasTotal ? Math.max(0, Math.min(100, Math.round(100 - (d.free_gb / d.total_gb) * 100))) : 0;
    const text = hasTotal
      ? t(lang, 'free_of', { free: formatGb(d.free_gb, locale), total: formatGb(d.total_gb, locale) })
      : t(lang, 'free_most', { free: formatGb(d.free_gb, locale) });
    return {
      label: d.label,
      path: d.path,
      text,
      hasTotal,
      used,
      default: library && d.default,
      library,
      note: library ? '' : t(lang, 'drive_no_library'),
      usb: d.bus === 'usb',
      icon: driveIcon(d.media),
      letter: d.letter,
      games: library && perDrive && d.letter ? perDrive.get(d.letter) || [] : null,
    };
  };
  return [...drives.map((d) => row(d, true)), ...others.map((d) => row(d, false))];
}

/** The service data of an install from the dialog: the drive picked when it offered a choice. */
export function installData(appid, drive) {
  return drive ? { appid, drive } : { appid };
}

/** The drives a first install may go to: those with a library and a drive letter, then the drives
 * without a library (newLibrary true: the PC creates a Steam library there), one per letter. Only
 * with at least two (one drive is no choice); [] otherwise. */
export function driveChoices(m) {
  const choices = allDrives(m);
  return choices.length >= 2 ? choices : [];
}

function allDrives(m) {
  const free = m && m.free;
  const drives = free && Array.isArray(free.drives) ? free.drives : [];
  const others = free && Array.isArray(free.noLibrary) ? free.noLibrary : [];
  const seen = new Set();
  return [
    ...drives.map((d) => ({ ...d, newLibrary: false })),
    ...others.map((d) => ({ ...d, newLibrary: true })),
  ].filter((d) => d.letter && !seen.has(d.letter) && seen.add(d.letter));
}

/** The drives an installed game may be moved to: those of driveChoices but its own (also a single
 * one); [] when its drive is not known (an older PC) or not there. */
export function moveChoices(m, game) {
  const own = game ? driveOf(game.drive) : '';
  if (!own || game.unavailable) return [];
  return allDrives(m).filter((d) => d.letter !== own[0].toLowerCase());
}

/** The reserve of the space check before an install: 10 % of the game size, at least 5 GB. Must
 * match pc/jobs/install.ps1 ($spaceReserveShare, $spaceReserveMin), which makes the final check. */
export const SPACE_RESERVE_SHARE = 0.1;
export const SPACE_RESERVE_MIN_GB = 5;

/** The space in GB a first install of a game needs, as install.ps1 counts it: its install size (in
 * MB) plus the reserve. 0 when the size is unknown. extraMb: what comes on top without a reserve of
 * its own (the workshop content a move copies along; move.ps1 counts it so). */
export function neededGb(sizeMb, extraMb = 0) {
  const size = Number(sizeMb);
  if (!Number.isFinite(size) || size <= 0) return 0;
  const gb = size / 1024;
  const extra = Math.max(0, Number(extraMb) || 0) / 1024;
  return gb + extra + Math.max(SPACE_RESERVE_MIN_GB, gb * SPACE_RESERVE_SHARE);
}

/** Whether a game of sizeMb fits on a drive with freeGb free, by the rule of install.ps1 (it stops
 * when the free space is below the size plus the reserve): { fits, needed (GB), missing (GB, 0
 * when it fits) }; null when the size or the free space is unknown (no check, as on the PC). */
export function driveFit(sizeMb, freeGb, extraMb = 0) {
  const needed = neededGb(sizeMb, extraMb);
  const free = Number(freeGb);
  if (!needed || !Number.isFinite(free) || free < 0) return null;
  return free >= needed ? { fits: true, needed, missing: 0 } : { fits: false, needed, missing: needed - free };
}

/** The drive a first install goes to when the dialog offers no choice: Steam's default library,
 * else the only drive with a library; null when that is not known. */
export function installDrive(m) {
  const drives = m && m.free && Array.isArray(m.free.drives) ? m.free.drives : [];
  return drives.find((d) => d.default) || (drives.length === 1 ? drives[0] : null);
}

/** The first drive of choices a game of sizeMb fits on: the drives with a library before those
 * without one, an SSD before others, otherwise in the order of choices; null when none fits or the
 * size is unknown. */
export function firstFittingDrive(choices, sizeMb, extraMb = 0) {
  const rank = (d) => (d.newLibrary ? 2 : 0) + (d.media === 'ssd' ? 0 : 1);
  const fitting = (choices || []).filter((d) => {
    const fit = driveFit(sizeMb, d.free_gb, extraMb);
    return fit && fit.fits;
  });
  return fitting.map((d, i) => [d, i]).sort((a, b) => rank(a[0]) - rank(b[0]) || a[1] - b[1]).map((x) => x[0])[0] || null;
}

/** The warning of an install dialog when the game probably does not fit on the drive it goes to;
 * '' when it fits or that is not known. Needed and missing rounded up like the drive rows (the PC
 * sends free_gb rounded down), so the numbers can never look equal. */
function noSpaceWarning(lang, locale, drive, sizeMb, extraMb = 0) {
  const fit = drive ? driveFit(sizeMb, drive.free_gb, extraMb) : null;
  if (!fit || fit.fits) return '';
  return t(lang, 'warn_no_space', {
    drive: drive.label,
    missing: formatGb(Math.ceil(fit.missing), locale),
    needed: formatGb(Math.ceil(fit.needed), locale),
    free: formatGb(drive.free_gb, locale),
  });
}

function timeOf(iso) {
  const time = new Date(String(iso || '')).getTime();
  return Number.isNaN(time) ? -Infinity : time;
}

/** The update history from the agent's history sensor (attribute entries, see PROTOCOL.md): newest
 * first (entries without a valid time last), at most limit. known is false while the PC never sent
 * a history. */
export function historyEntries(state, limit = HISTORY_LIMIT) {
  const entries = attrs(state).entries;
  if (!Array.isArray(entries)) return { known: false, entries: [] };
  const list = entries
    .filter((e) => e && typeof e === 'object' && (e.name || validAppid(e.appid)))
    .map((e) => ({
      time: e.time ? String(e.time) : '',
      appid: validAppid(e.appid) ? String(e.appid).trim() : '',
      name: String(e.name || ''),
      kind: String(e.kind || ''),
      source: String(e.source || ''),
      oldBuild: e.old_build ? String(e.old_build) : '',
      newBuild: e.new_build ? String(e.new_build) : '',
    }))
    .map((e, index) => ({ e, index, time: timeOf(e.time) }))
    .sort((a, b) => (b.time - a.time) || (a.index - b.index))
    .map((x) => x.e);
  return { known: true, entries: list.slice(0, Math.max(0, limit)) };
}

/** Everything the card shows, from hass.states and the entity IDs of steam_engine/entities;
 * timeZone is Home Assistant's (hass.config.time_zone), in which the active hours are shown, images
 * the resolved image URLs (normalizeImages; the running game's picture prefers them). */
export function buildModel(states, ids, timeZone = '', images = null) {
  const own = (ids && ids.entities) || {};
  const agent = (ids && ids.agent) || {};
  const st = (id) => (id && states ? states[id] : undefined);
  const updatesState = st(own.updates);
  const library = libraryGames(st(agent.library));
  const updates = updateGames(updatesState);
  const notInstalled = notInstalledGames(st(own.not_installed));
  const waiting = deferredWork(updatesState);
  const job = jobInfo(st(own.download_progress), st(agent.install_status), waiting.appids);
  // A game handed to the PC or running is not waiting (whatever the attribute says)
  const deferred = new Set([...waiting.appids].filter((id) => !job.job.includes(id) && id !== job.appid));
  const imageMap = images && typeof images === 'object' ? images : {};
  const current = currentGame(st(own.current_game));
  // The sensor's picture is the CDN's old path, which newer games do not have
  if (current) current.picture = steamImageUrl(imageMap, current.appid, 'header', current.picture);
  const names = new Map();
  for (const game of [...notInstalled.games, ...updates, ...(library || [])]) {
    if (game.name) names.set(game.appid, game.name);
  }
  if (current && current.name) names.set(current.appid, current.name);
  const pc = st(own.pc);
  const pcState = st(own.pc_state);
  const purchases = st(own.auto_install_purchases);
  const isOn = (id) => {
    const s = st(id);
    return !!s && s.state === 'on';
  };
  // A button that does not exist or is unavailable (agent offline) would be pressed without effect
  // and without an error, so it counts as unknown
  const usable = (id) => {
    const s = st(id);
    return !!s && s.state !== 'unavailable' ? id : '';
  };
  // A binary sensor without a state (missing, unknown, unavailable) is null: its line is left out
  const onOff = (id) => (hasState(st(id)) ? isOn(id) : null);
  const autoOff = st(own.auto_off_enabled);
  const safe = st(own.safe_to_shut_down);
  const lastInput = st(agent.last_input);
  const network = st(agent.network);
  const steamBusySensor = isOn(own.steam_busy);
  const steamDownloading = isOn(agent.steam_downloading);
  const move = moveInfo(st(own.download_progress));
  return {
    pcOn: !!pc && pc.state === 'on',
    // The PC state sensor: off, starting, on, shutting_down or restarting ('' while unknown)
    pcPhase: pcPhaseOf(pcState),
    agentOnline: isOn(agent.agent_online),
    steamBusy: job.active || steamBusySensor || steamDownloading,
    gameRunning: !!current || isOn(agent.game_running),
    // What keeps the PC busy, for the status lines and the warnings of a forced shut down: a job of
    // the integration (also a move to another drive), or Steam downloading by itself (Steam busy
    // without our job is that, or a job being handed over)
    jobRunning: job.active || !!move,
    steamDownloading: steamDownloading || (steamBusySensor && !job.active && !move),
    pluginBusy: isOn(own.plugin_busy),
    pluginBusyBy: asIds(attrs(st(own.plugin_busy)).busy_by),
    steamRunning: onOff(agent.steam_running),
    safe: onOff(own.safe_to_shut_down),
    offAt: hasState(safe) && attrs(safe).off_at ? String(attrs(safe).off_at) : '',
    lastInput: hasState(lastInput) ? String(lastInput.state) : '',
    network: hasState(network) ? { value: network.state, unit: String(attrs(network).unit_of_measurement || '') } : null,
    // Entities of the PC section; an empty one (e.g. a core without the buttons, or a button that
    // is missing or unavailable) hides its button
    pcEntity: own.pc || '',
    restartEntity: usable(agent.restart_button),
    lockEntity: usable(agent.lock_button),
    forceShutdownEntity: usable(agent.shutdown_button),
    closeGamesEntity: usable(agent.close_games_button),
    autoOffEntity: own.auto_off_enabled || '',
    autoOffOn: !!autoOff && autoOff.state === 'on',
    autoOffKnown: !!autoOff,
    history: historyEntries(st(agent.history)),
    library,
    updates,
    autoUpdate: autoUpdateSet(updatesState),
    muted: mutedSet(updatesState),
    checkRunning: attrs(updatesState).check_running === true,
    // Active hours: the window ({ start, end } or null), the next start while automatic work waits
    // for it ('' otherwise) and the waiting games. Waiting games are not in open (the queue above
    // leaves them out): a tap on Update or Install starts them at once.
    activeHours: activeHours(updatesState),
    timeZone: String(timeZone || ''),
    deferredUntil: waiting.until,
    deferred,
    notInstalled,
    job,
    open: new Set([...job.job, ...job.queue]),
    // A move to another drive that runs (null when none) and the last one's end
    move,
    moveResult: moveResultOf(st(own.download_progress)),
    current,
    playing: current ? current.appid : '',
    purchasesEntity: own.auto_install_purchases || '',
    purchasesOn: !!purchases && purchases.state === 'on',
    purchasesKnown: !!purchases,
    free: freeSpace(st(agent.library_free)),
    names,
    images: imageMap,
  };
}

/** The entity IDs whose change renders the card again: every entity buildModel reads. */
export function watchedIds(ids) {
  const own = (ids && ids.entities) || {};
  const agent = (ids && ids.agent) || {};
  return [
    own.pc, own.pc_state, own.updates, own.not_installed, own.download_progress, own.current_game,
    own.auto_install_purchases, own.steam_busy, agent.library, agent.install_status, agent.library_free,
    agent.agent_online, agent.steam_downloading, agent.game_running, agent.restart_button,
    agent.lock_button, own.auto_off_enabled, own.safe_to_shut_down, own.plugin_busy,
    agent.steam_running, agent.last_input, agent.network, agent.history, agent.shutdown_button,
    agent.close_games_button,
  ].filter(Boolean);
}

export function gameName(lang, names, appid) {
  return (names && names.get(String(appid))) || t(lang, 'game_fallback', { appid });
}

/** Which controls a row shows.
 * list: 'installed', 'updates' or 'not_installed'; open: app IDs in the job or the queue. */
export function rowActions({ list, game, playing, pcOn, actionsWhenPcOff, open, move }) {
  const canAct = !!pcOn || actionsWhenPcOff !== false;
  const none = { launch: false, uninstall: false, autoUpdate: false, update: false, install: false };
  const queued = !!open && open.has(game.appid);
  if (list === 'installed') {
    if (game.unavailable) return none;
    const notPlaying = game.appid !== playing;
    // While a move runs no game is started (Steam closes), and the game being moved keeps only its switch
    const moving = !!move && String(move.appid) === String(game.appid);
    return { ...none, autoUpdate: true, launch: canAct && notPlaying && !move, uninstall: canAct && notPlaying && !moving };
  }
  if (list === 'updates') return { ...none, update: canAct && !queued };
  if (list === 'not_installed') return { ...none, install: canAct && !queued };
  return none;
}

/** Which buttons the PC section shows. Wake only while the PC is off (also when actions_when_pc_off
 * is off); shut down, restart, lock and the forced shut down only while the agent is online: it
 * carries them out, and its buttons are unavailable without it, so a press would be skipped without
 * an error. Close games also only while a game runs. A button whose entity is missing or
 * unavailable is not shown. */
export function pcActions(m) {
  const agentOnline = !!m.agentOnline;
  return {
    wake: !!m.pcEntity && !m.pcOn,
    shutdown: !!m.pcEntity && agentOnline,
    restart: !!m.restartEntity && agentOnline,
    lock: !!m.lockEntity && agentOnline,
    close_games: !!m.closeGamesEntity && agentOnline && !!m.gameRunning,
    force_shutdown: !!m.forceShutdownEntity && agentOnline,
  };
}

/** The PC actions that ask first. */
export const PC_CONFIRM = ['shutdown', 'restart', 'force_shutdown', 'close_games'];

/** The PC action of a confirm dialog's request (pc_<action>), or '' for a game's dialog. */
export function pcDialogAction(requestAction) {
  const text = String(requestAction || '');
  const action = text.startsWith('pc_') ? text.slice(3) : '';
  return PC_CONFIRM.includes(action) ? action : '';
}

const PC_PHASES = ['off', 'starting', 'on', 'shutting_down', 'restarting'];

/** The state of the PC state sensor, '' while it is missing or unknown. */
export function pcPhaseOf(state) {
  return state && PC_PHASES.includes(state.state) ? state.state : '';
}

// The PC buttons that stay busy while the PC state says their action is on its way: the PC switch
// only turns on once the PC is reachable or its agent reports, often a minute into the boot, and a
// second press of a power-button device during the boot could switch the PC off again
const PHASE_ACTIONS = {
  starting: ['wake'],
  shutting_down: ['shutdown', 'force_shutdown'],
  restarting: ['restart'],
};

/** True while the PC state says the action is on its way (the button shows busy, a tap does nothing). */
export function pcActionPending(m, action) {
  return !!m && (PHASE_ACTIONS[m.pcPhase] || []).includes(action);
}

/** How the PC looks in the status chip and the PC section: { tone, icon, text (key), pulse }. On its
 * way (starting, shutting down, restarting) it is blue with a pulsing icon; without the PC state
 * sensor the PC switch tells on or off. */
export function pcLook(m) {
  const looks = {
    starting: { tone: 'blue', icon: 'mdi:power', text: 'pc_starting', pulse: true },
    shutting_down: { tone: 'blue', icon: 'mdi:power-off', text: 'pc_shutting_down', pulse: true },
    restarting: { tone: 'blue', icon: 'mdi:restart', text: 'pc_restarting', pulse: true },
  };
  return looks[m.pcPhase] || (m.pcOn
    ? { tone: 'green', icon: 'mdi:power', text: 'pc_on', pulse: false }
    : { tone: 'neutral', icon: 'mdi:power-off', text: 'pc_off', pulse: false });
}

/** The message when the PC state left an action without its result ('' otherwise): starting that
 * went back to off, shutting down back to on, restarting that ended without the agent back. */
export function phaseFailure(before, now) {
  if (!before || !now || before === now) return '';
  const failed = (before === 'starting' && now === 'off')
    || (before === 'shutting_down' && now === 'on')
    || (before === 'restarting' && now !== 'on');
  return failed ? `msg_phase_failed_${before}` : '';
}

/** Text keys of the warnings in the confirmation of a PC action: a running game is closed by a
 * shut down or restart; a busy Steam keeps the PC on for a shut down (the dialog text says so), but
 * a restart interrupts it. The forced shut down names everything it does not wait for that applies
 * right now: a game, a job of the integration, a download of Steam, a busy plugin. */
export function pcWarnings(action, m) {
  const warnings = [];
  if (action === 'close_games') return warnings;
  if (m.gameRunning) warnings.push('warn_game');
  if (action === 'restart' && m.steamBusy) warnings.push('warn_steam');
  if (action === 'force_shutdown') {
    if (m.jobRunning) warnings.push('warn_force_job');
    if (m.steamDownloading) warnings.push('warn_force_download');
    if (m.pluginBusy) warnings.push(m.pluginBusyBy && m.pluginBusyBy.length ? 'warn_plugin_named' : 'warn_plugin');
  }
  return warnings;
}

/** The texts of the confirmation of a PC action: title, text, warnings and the confirm button.
 * Close games names the running game when it is known; the agent closes every running game. */
export function pcConfirm(lang, action, m) {
  const vars = { names: ((m && m.pluginBusyBy) || []).join(', ') };
  const warnings = pcWarnings(action, m || {}).map((key) => t(lang, key, vars));
  if (action === 'close_games') {
    const name = m && m.current && m.current.name;
    return {
      title: name ? t(lang, 'confirm_close_games_title_named', { name }) : t(lang, 'confirm_close_games_title'),
      text: t(lang, 'confirm_close_games_text'),
      warnings,
      ok: t(lang, 'action_close_games'),
    };
  }
  return {
    title: t(lang, `confirm_${action}_title`),
    text: t(lang, `confirm_${action}_text`),
    warnings,
    ok: t(lang, `action_${action}`),
  };
}

/** The status lines of the PC section as [label, value], in this order: Steam running, Steam
 * busy, last input, network, safe to shut down, planned auto off, active hours, busy plugin. A line whose data
 * is not known (sensor missing, unknown or unavailable, e.g. while the agent is offline) is left
 * out. Whether the agent is online stays in the section's first line. */
export function pcFacts(lang, m, locale, now = new Date()) {
  return pcFactItems(lang, m, locale, now).map((f) => [f.label, f.value]);
}

/** The same status lines as pcFacts, each as { key, label, value, warn }: key names the line
 * (steam, steam_busy, last_input, network, safe, off_at, active_hours, plugin) for a style that shows an icon
 * per line; warn marks what keeps the PC from shutting down (not safe, a busy plugin). */
export function pcFactItems(lang, m, locale, now = new Date()) {
  const facts = [];
  const add = (key, labelKey, value, warn = false) => facts.push({ key, label: t(lang, labelKey), value, warn });
  if (m.steamRunning !== null && m.steamRunning !== undefined) {
    add('steam', 'fact_steam', t(lang, m.steamRunning ? 'value_running' : 'value_not_running'));
  }
  if (m.agentOnline || m.steamBusy) {
    const busy = m.jobRunning ? 'value_job' : (m.steamDownloading || m.steamBusy ? 'value_downloading' : 'value_no');
    add('steam_busy', 'fact_steam_busy', t(lang, busy));
  }
  const input = formatRelative(m.lastInput, locale, now);
  if (input) add('last_input', 'fact_last_input', input);
  const rate = m.network ? formatRate(m.network.value, m.network.unit, locale) : '';
  if (rate) add('network', 'fact_network', rate);
  if (m.safe !== null && m.safe !== undefined) add('safe', 'fact_safe', t(lang, m.safe ? 'value_yes' : 'value_no'), !m.safe);
  const offAt = formatSince(m.offAt, locale, now);
  if (offAt) {
    add('off_at', 'fact_off_at', t(lang, 'off_at_value', { time: offAt, relative: formatRelative(m.offAt, locale, now) }));
  }
  const hours = formatWindow(lang, m.activeHours, locale);
  if (hours) add('active_hours', 'fact_active_hours', hours);
  if (m.pluginBusy) {
    add('plugin', 'fact_plugin', m.pluginBusyBy && m.pluginBusyBy.length ? m.pluginBusyBy.join(', ') : t(lang, 'value_yes'), true);
  }
  return facts;
}

/** One short line for the PC area while something keeps the PC from being shut down, so a refused
 * shut down does not surprise while the status details are closed: a job of the integration, Steam
 * downloading by itself, busy plugins (what the PC switch waits for). '' while the PC is off or
 * nothing holds it. Not "safe to shut down" alone: that is also off after any recent input. */
export function pcHold(lang, m) {
  if (!m || !m.pcOn) return '';
  const reasons = [];
  if (m.jobRunning) reasons.push(t(lang, 'value_job'));
  else if (m.steamDownloading) reasons.push(t(lang, 'chip_steam_downloading'));
  if (m.pluginBusy) reasons.push(...(m.pluginBusyBy && m.pluginBusyBy.length ? m.pluginBusyBy : [t(lang, 'hold_plugin')]));
  return reasons.length ? t(lang, 'hold', { reasons: reasons.join(', ') }) : '';
}

/** The hint while automatic work waits for the active hours: { text, names } ("Waiting for active
 * hours – starts at 08:00" and the names of the waiting games), or null while nothing waits. */
export function deferredHint(lang, m, locale, now = new Date()) {
  const ids = m && m.deferred instanceof Set ? [...m.deferred] : [];
  if (!m || (!m.deferredUntil && !ids.length)) return null;
  const when = formatDeferredStart(lang, m.deferredUntil, locale, now, m.timeZone);
  const text = when ? `${t(lang, 'deferred_waiting')} – ${when}` : t(lang, 'deferred_waiting');
  return { text, names: ids.map((id) => gameName(lang, m.names, id)).join(', ') };
}

/** The game waits for the active hours: in the waiting work and not running. */
function isDeferred(m, appid, running) {
  return !running && !!m && m.deferred instanceof Set && m.deferred.has(appid);
}

/** The PC status details are open: remembered in this browser as '1', closed otherwise. */
export function detailsOpen(stored) {
  return stored === '1';
}

/** The icon of each PC action (buttons and confirmations). */
export const PC_ICONS = {
  wake: 'mdi:power',
  shutdown: 'mdi:power-off',
  restart: 'mdi:restart',
  lock: 'mdi:lock-outline',
  close_games: 'mdi:close-box-multiple-outline',
  force_shutdown: 'mdi:power-plug-off-outline',
};

/** The status chips: PC on or off, the download (of the job, or of Steam by itself), the number
 * of updates and the free space. Each as { key, tone, icon, text }; tone is one of green (on,
 * fine), blue (a job, something to do), amber (attention), red (danger), neutral. */
export function statusChips(lang, m, locale) {
  const chips = [];
  const look = pcLook(m);
  chips.push({ key: 'pc', tone: look.tone, icon: look.icon, text: t(lang, look.text), pulse: look.pulse });
  if (m.job && m.job.appid) {
    chips.push({ key: 'download', tone: 'blue', icon: 'mdi:download', text: t(lang, 'chip_downloading', { percent: m.job.percent }) });
  } else if (m.steamDownloading && !m.move) {
    chips.push({ key: 'download', tone: 'blue', icon: 'mdi:download', text: t(lang, 'chip_steam_downloading') });
  }
  if (m.move) chips.push({ key: 'move', tone: 'blue', icon: 'mdi:folder-move-outline', text: t(lang, 'chip_moving', { percent: m.move.percent }) });
  const counted = countedUpdates(m).length;
  chips.push(counted
    ? { key: 'updates', tone: 'blue', icon: 'mdi:update', text: tn(lang, 'updates_count', counted) }
    : { key: 'updates', tone: 'green', icon: 'mdi:check-circle-outline', text: t(lang, 'all_up_to_date') });
  if (m.free && Number.isFinite(m.free.free)) {
    chips.push({ key: 'free', tone: 'neutral', icon: 'mdi:harddisk', text: t(lang, 'free_most', { free: formatGb(m.free.free, locale) }) });
  }
  return chips;
}

/** The state tags of a game row: { tone, tags } for the list installed, updates or not_installed.
 * The tone follows the most important state (playing, in the running job, waiting for the active
 * hours, queued, has an update, new purchase); tags are { key, tone, vars } of text keys and are what the row shows. A game on a
 * missing drive is neutral without tags (its row is dimmed and says so). */
export function rowState(list, game, m) {
  const tags = [];
  const tag = (key, tone, vars) => tags.push({ key, tone, vars });
  const job = (m && m.job) || {};
  const open = (m && m.open) || new Set();
  const running = !!job.active && job.appid === game.appid;
  const deferred = isDeferred(m, game.appid, running);
  const queued = open.has(game.appid) && !running && !deferred;
  let tone = 'neutral';
  if (list === 'installed' && game.unavailable) return { tone, tags };
  if (list === 'installed' && m && game.appid === m.playing) {
    tone = 'green';
    tag('badge_playing', 'green');
  }
  if (list === 'installed' && m && m.move && m.move.appid === game.appid) {
    // No percent either (see below)
    if (tone === 'neutral') tone = 'blue';
    tag('tag_moving', 'blue');
  }
  if (running) {
    if (tone === 'neutral') tone = 'blue';
    // No percent here: the row would change on every progress tick and the panel would be
    // rebuilt under the finger (lost tap). The percent lives in the header job tile and chip.
    tag('tag_downloading', 'blue');
  } else if (deferred) {
    // Calm: the row keeps the tone of its list (an update stays amber), the tag says it waits
    tag('badge_deferred', 'neutral');
  } else if (queued) {
    if (tone === 'neutral') tone = 'blue';
    tag('badge_queued', 'neutral');
  }
  if (list === 'updates') {
    if (tone === 'neutral' && !game.muted) tone = 'amber';
    if (game.auto) tag('badge_auto', 'green');
    if (game.muted) tag('badge_muted', 'neutral');
  } else if (list === 'installed' && m && countedUpdates(m).some((g) => g.appid === game.appid)) {
    if (tone === 'neutral') tone = 'amber';
    if (!running && !queued && !deferred) tag('tag_update', 'amber');
  } else if (list === 'not_installed' && game.new) {
    if (tone === 'neutral') tone = 'blue';
    tag('badge_new', 'blue');
  }
  return { tone, tags };
}

/** The content of a confirmation: { title, text, warnings, ok, cancel, tone, icon, okTone, okIcon,
 * image, drives, driveLabel }. request is the dialog's request: { action: 'uninstall' | 'install',
 * appid, name, size } of the row tapped (size in MB: the installed size of a game to uninstall, the
 * install size of steam_engine/game_size of a game to install, 0 when unknown), or
 * { action: 'pc_<action>' }. The image is the game's
 * header picture when images are on (the resolved one from m.images when there is one). A game's
 * text says when the PC is switched on for it. drives: for an install with at least two drives to
 * pick from (driveChoices), the choice of the target drive: { letter, label, free (text), note (the
 * hint on a drive without a library, else ''), usb (a USB drive), media ('SSD', 'HDD' or ''), fits
 * (true, false, null while the install size is unknown), fit (its text: "fits", "does not fit (31
 * GB missing)", ''), default, checked }; [] otherwise. Checked is the drive request.drive names
 * (picked in the dialog), else the preselection: Steam's default drive (the first one when none is
 * marked) when the game fits there or the size is unknown, else the first drive it fits on
 * (firstFittingDrive), named in driveHint ("F: is too full, E: preselected."; '' otherwise and
 * once another drive is picked); when it fits nowhere the default stays. warnings of an install:
 * the game probably does not fit on the drive checked, or without a choice on the drive it goes to
 * (installDrive); installing stays possible, the PC makes the final check. */
export function confirmContent(lang, request, m, config, locale) {
  const cancel = t(lang, 'cancel');
  const pc = pcDialogAction(request.action);
  if (pc) {
    const texts = pcConfirm(lang, pc, m || {});
    return {
      ...texts, cancel, tone: 'red', icon: PC_ICONS[pc], okTone: 'danger', okIcon: PC_ICONS[pc], image: '', drives: [], driveLabel: '', driveHint: '',
    };
  }
  if (request.action === 'move') return moveConfirm(lang, request, m, config, locale);
  const uninstall = request.action === 'uninstall';
  // An uninstall deletes the game's workshop content too (uninstall.ps1): it is counted along as a
  // move does, but only with a known size of the game, so the size named is never the workshop's alone
  const library = uninstall && m && Array.isArray(m.library) ? m.library : [];
  const game = library.find((g) => g.appid === String(request.appid)) || null;
  const workshopMb = game && Number(request.size) > 0 ? game.workshop_mb || 0 : 0;
  const size = formatSize(Number(request.size) + workshopMb, locale);
  let text;
  if (uninstall) {
    const key = workshopMb ? 'confirm_uninstall_text_workshop' : 'confirm_uninstall_text';
    text = size ? t(lang, key, { size }) : t(lang, 'confirm_uninstall_text_nosize');
  } else text = size ? t(lang, 'confirm_install_text_size', { size }) : t(lang, 'confirm_install_text');
  if (m && !m.pcOn) text += ` ${t(lang, 'confirm_pc_off')}`;
  const choices = uninstall ? [] : driveChoices(m);
  const sizeMb = uninstall ? 0 : request.size;
  const star = choices.find((d) => d.default) || choices[0] || null;
  const starFit = star ? driveFit(sizeMb, star.free_gb) : null;
  const better = starFit && !starFit.fits ? firstFittingDrive(choices, sizeMb) : null;
  const preselected = better || star;
  const picked = choices.find((d) => request.drive && d.letter === request.drive) || preselected;
  const driveHint = better && picked === better ? t(lang, 'drive_preselected', { full: star.label, drive: better.label }) : '';
  const drives = choices.map((d) => driveOption(lang, locale, d, d === picked, sizeMb));
  const warning = uninstall ? '' : noSpaceWarning(lang, locale, choices.length ? picked : installDrive(m), sizeMb);
  return {
    title: t(lang, uninstall ? 'confirm_uninstall_title' : 'confirm_install_title', { name: request.name }),
    text,
    warnings: warning ? [warning] : [],
    ok: t(lang, uninstall ? 'action_uninstall' : 'action_install'),
    cancel,
    tone: uninstall ? 'red' : 'blue',
    icon: uninstall ? 'mdi:delete-outline' : 'mdi:download',
    okTone: uninstall ? 'danger' : 'fill',
    okIcon: uninstall ? 'mdi:delete-outline' : 'mdi:download',
    image: config && config.show_images && request.appid ? steamImageUrl(m && m.images, request.appid, 'header') : '',
    drives,
    driveLabel: drives.length ? t(lang, 'install_to') : '',
    driveHint,
  };
}

/** One drive row of the install or move dialog (see confirmContent). */
function driveOption(lang, locale, d, checked, sizeMb, extraMb = 0) {
  const fit = driveFit(sizeMb, d.free_gb, extraMb);
  let fitText = '';
  if (fit) fitText = fit.fits ? t(lang, 'drive_fits') : t(lang, 'drive_no_fit', { missing: formatGb(Math.ceil(fit.missing), locale) });
  return {
    letter: d.letter,
    label: d.label,
    free: Number.isFinite(d.free_gb) ? t(lang, 'free_most', { free: formatGb(d.free_gb, locale) }) : '',
    note: d.newLibrary ? t(lang, 'drive_new_library') : '',
    usb: d.bus === 'usb',
    media: d.media ? t(lang, `drive_${d.media}`) : '',
    fits: fit ? fit.fits : null,
    fit: fitText,
    default: d.default,
    checked,
  };
}

/** The confirmation of a move ({ action: 'move', appid, name, drive? }): the game is looked up in
 * the library for its drive and its size with the workshop content; the drives are the others
 * (moveChoices), with the fit by the rule of move.ps1 (the size of the game and its workshop content
 * plus the reserve of an install). Checked is the drive picked, else the first one the game fits on
 * (drives with a library first, an SSD before others), else the first one; the warning when it
 * probably does not fit on the drive checked. */
function moveConfirm(lang, request, m, config, locale) {
  const library = m && Array.isArray(m.library) ? m.library : [];
  const game = library.find((g) => g.appid === String(request.appid)) || null;
  const sizeMb = game ? game.size_mb : 0;
  const extraMb = game ? game.workshop_mb || 0 : 0;
  const size = formatSize(sizeMb + extraMb, locale);
  const from = game ? driveOf(game.drive) : '';
  let text = t(lang, size ? 'confirm_move_text_size' : 'confirm_move_text', { size, drive: from || '?' });
  if (m && !m.pcOn) text += ` ${t(lang, 'confirm_pc_off')}`;
  const choices = moveChoices(m, game);
  const picked = choices.find((d) => request.drive && d.letter === request.drive)
    || firstFittingDrive(choices, sizeMb, extraMb) || choices[0] || null;
  const drives = choices.map((d) => driveOption(lang, locale, d, d === picked, sizeMb, extraMb));
  const warning = noSpaceWarning(lang, locale, picked, sizeMb, extraMb);
  return {
    title: t(lang, 'confirm_move_title', { name: request.name }),
    text,
    warnings: warning ? [warning] : [],
    ok: t(lang, 'action_move'),
    cancel: t(lang, 'cancel'),
    tone: 'blue',
    icon: 'mdi:folder-move-outline',
    okTone: 'fill',
    okIcon: 'mdi:folder-move-outline',
    image: config && config.show_images && request.appid ? steamImageUrl(m && m.images, request.appid, 'header') : '',
    drives,
    driveLabel: drives.length ? t(lang, 'move_to') : '',
    driveHint: '',
  };
}

// --- library style: grid tiles and the game sheet ---------------------------------------------

/** The icon of each state flag on a capsule of the library style. */
export const FLAG_ICONS = {
  badge_playing: 'mdi:play',
  tag_downloading: 'mdi:download',
  tag_moving: 'mdi:folder-move-outline',
  badge_queued: 'mdi:timer-sand',
  badge_deferred: 'mdi:clock-outline',
  tag_update: 'mdi:update',
  badge_new: 'mdi:new-box',
  badge_muted: 'mdi:bell-off-outline',
  tag_drive_missing: 'mdi:harddisk-remove',
  not_installed: 'mdi:download-off-outline',
};

function flag(lang, key, tone) {
  return { key, tone, icon: FLAG_ICONS[key] || '', text: t(lang, key) };
}

/** One capsule of the library style's grid for a game of the list installed, updates or
 * not_installed: { appid, name, list, off, update, auto, flags, meta, label }. flags are the state
 * badges on the image ({ key, tone, icon, text }, the most important first; automatic updates are a
 * mark in the meta line instead), meta the short line under the name, label the accessible name of
 * the tile. Nothing in it changes with the progress of a job, so a progress tick does not touch the
 * grid. */
export function gridTile(lang, locale, list, game, m) {
  const name = game.name || gameName(lang, m && m.names, game.appid);
  const state = rowState(list, game, m);
  const flags = state.tags.filter((tag) => tag.key !== 'badge_auto').map((tag) => flag(lang, tag.key, tag.tone));
  const busy = flags.some((f) => ['tag_downloading', 'tag_moving', 'badge_queued', 'badge_deferred'].includes(f.key));
  if (list === 'updates' && !busy) flags.push(flag(lang, 'tag_update', 'amber'));
  const off = list === 'installed' && game.unavailable === true;
  if (off) flags.push(flag(lang, 'tag_drive_missing', 'neutral'));
  const update = list === 'updates' || (list === 'installed' && !off && countedUpdates(m).some((g) => g.appid === game.appid));
  const auto = list !== 'not_installed' && !off && !!m && !!m.autoUpdate && m.autoUpdate.has(game.appid);
  const meta = [];
  if (list === 'installed') {
    meta.push(formatSize(game.size_mb, locale));
    if (!off) {
      const date = formatDate(game.last_played, locale);
      meta.push(date ? t(lang, 'last_played', { date }) : t(lang, 'never_played'));
    }
  } else if (list === 'updates') {
    const size = formatSize(game.size_mb, locale);
    if (size) meta.push(t(lang, 'update_size', { size }));
  }
  const shownMeta = meta.filter(Boolean);
  // The capsule's short line keeps the size alone (the sheet names the drive), its label says both
  const spoken = list === 'installed' ? [sizeOnDrive(lang, locale, game), ...shownMeta.slice(1)].filter(Boolean) : shownMeta;
  const label = [name, ...flags.map((f) => f.text), ...spoken, ...(auto ? [t(lang, 'auto_update')] : [])].join(', ');
  return { appid: game.appid, name, list, off, update, auto, flags, meta: shownMeta, label };
}

/** Everything the sheet of one game shows, from the current model: the game in the library
 * (installed), in the updates (update) and among the owned games (owned), each null when it is not
 * there, and its state. known is false once the game is in none of them (it was uninstalled and is
 * not owned): the sheet closes then. */
export function gameDetail(lang, appid, m) {
  const id = String(appid === undefined || appid === null ? '' : appid);
  const find = (list) => (Array.isArray(list) ? list.find((g) => g.appid === id) : null) || null;
  const installed = find(m && m.library);
  const update = find(m && m.updates);
  const owned = find(m && m.notInstalled && m.notInstalled.games);
  const name = (installed && installed.name) || (update && update.name) || (owned && owned.name)
    || gameName(lang, m && m.names, id);
  const job = (m && m.job) || {};
  const running = !!job.active && job.appid === id;
  const open = (m && m.open) || new Set();
  const deferred = isDeferred(m, id, running);
  return {
    appid: id,
    name,
    known: !!(installed || update || owned),
    installed,
    update,
    owned,
    playing: !!m && !!id && id === m.playing,
    running,
    moving: !!m && !!m.move && m.move.appid === id,
    queued: open.has(id) && !running && !deferred,
    deferred,
    auto: !!m && !!m.autoUpdate && m.autoUpdate.has(id),
    muted: !!m && !!m.muted && m.muted.has(id),
    unavailable: !!installed && installed.unavailable === true,
  };
}

/** The actions of a game's sheet, in this order: launch, update, auto (the switches of the automatic
 * updates and of muting them), move, uninstall for an installed game; install for an owned game that is not installed.
 * The same rules as the rows of the tiles style (rowActions): nothing for the running game but its
 * switch, nothing on a missing drive, no update or install while the game is in the job or the
 * queue, and only the switch while the PC is off with actions_when_pc_off off. Move only where
 * uninstall is offered, while no Steam job and no other move runs, and when the PC reports another
 * drive for it (moveChoices). While a move runs no game is started (Steam closes for a moment), and
 * the game being moved has only its switch. */
export function sheetActions(detail, m, config) {
  if (!detail || !m) return [];
  const base = {
    playing: m.playing,
    pcOn: m.pcOn,
    actionsWhenPcOff: !config || config.actions_when_pc_off !== false,
    open: m.open,
  };
  const actions = [];
  const lib = detail.installed ? rowActions({ ...base, list: 'installed', game: detail.installed }) : null;
  if (detail.moving) return lib && lib.autoUpdate ? ['auto'] : [];
  if (lib && lib.launch && !m.move) actions.push('launch');
  if (detail.update && !detail.unavailable && rowActions({ ...base, list: 'updates', game: detail.update }).update) {
    actions.push('update');
  }
  if (lib && lib.autoUpdate) actions.push('auto');
  // HA refuses a move while a game runs, a job runs or is handed over, or the queue waits
  const jobBusy = !!m.job && (!!m.job.active || !!(m.job.queue && m.job.queue.length));
  if (lib && lib.uninstall && !m.move && !jobBusy && !m.gameRunning
    && moveChoices(m, detail.installed).length) actions.push('move');
  if (lib && lib.uninstall) actions.push('uninstall');
  if (!detail.installed && !detail.update && detail.owned
    && rowActions({ ...base, list: 'not_installed', game: detail.owned }).install) {
    actions.push('install');
  }
  return actions;
}

/** The state flags of a game's sheet ({ key, tone, icon, text }): running, in the job, waiting for
 * the active hours or queued, an update, a new purchase, not installed, a missing drive. */
export function sheetFlags(lang, detail) {
  const flags = [];
  if (detail.playing) flags.push(flag(lang, 'badge_playing', 'green'));
  if (detail.moving) flags.push(flag(lang, 'tag_moving', 'blue'));
  if (detail.running) flags.push(flag(lang, 'tag_downloading', 'blue'));
  else if (detail.deferred) flags.push(flag(lang, 'badge_deferred', 'neutral'));
  else if (detail.queued) flags.push(flag(lang, 'badge_queued', 'neutral'));
  if (detail.update && !detail.running && !detail.queued && !detail.deferred) flags.push(flag(lang, 'tag_update', 'amber'));
  if (!detail.installed && !detail.update && detail.owned) {
    if (detail.owned.new) flags.push(flag(lang, 'badge_new', 'blue'));
    flags.push(flag(lang, 'not_installed', 'neutral'));
  }
  if (detail.unavailable) flags.push(flag(lang, 'tag_drive_missing', 'neutral'));
  return flags;
}

/** Whether a game's sheet shows the install size right of the name (asked from steam_engine/game_size):
 * an owned game that is not installed. An installed game shows its size in the info line. */
export function wantsGameSize(detail) {
  return !!detail && !detail.installed && !detail.update && !!detail.owned;
}

/** The info line of a game's sheet: size with its drive, last played (or the missing drive) and
 * the size of a pending update, as texts. */
export function sheetMeta(lang, locale, detail) {
  const meta = [];
  if (detail.installed) {
    meta.push(sizeOnDrive(lang, locale, detail.installed));
    if (detail.unavailable) {
      meta.push(t(lang, 'drive_unavailable'));
    } else {
      const date = formatDate(detail.installed.last_played, locale);
      meta.push(date ? t(lang, 'last_played', { date }) : t(lang, 'never_played'));
    }
  }
  if (detail.update) {
    const size = formatSize(detail.update.size_mb, locale);
    if (size) meta.push(t(lang, 'update_size', { size }));
  }
  return meta.filter(Boolean);
}

/** The service call of a PC action: the integration's PC switch for wake and shut down (it refuses
 * the shut down while Steam or a plugin keeps the PC busy), the agent's buttons for restart, lock,
 * close games and the forced shut down (the agent shuts down at once, the integration's checks do
 * not apply). null when the entity is unknown. */
export function pcServiceCall(action, m) {
  const calls = {
    wake: ['switch', 'turn_on', m.pcEntity],
    shutdown: ['switch', 'turn_off', m.pcEntity],
    restart: ['button', 'press', m.restartEntity],
    lock: ['button', 'press', m.lockEntity],
    close_games: ['button', 'press', m.closeGamesEntity],
    force_shutdown: ['button', 'press', m.forceShutdownEntity],
  };
  const call = calls[action];
  if (!call || !call[2]) return null;
  return { domain: call[0], service: call[1], target: { entity_id: call[2] } };
}

const SENT_KEYS = {
  install: 'msg_install_sent',
  update: 'msg_update_sent',
  uninstall: 'msg_uninstall_sent',
  launch: 'msg_launch_sent',
  move: 'msg_move_sent',
};

/** The message for an action's answer (return_response). track: watch for the end of the job.
 * null when there is nothing to say (auto update or muting switched as asked). */
export function responseMessage(lang, action, name, response) {
  const r = response && typeof response === 'object' ? response : {};
  if (action === 'check') {
    if (r.skipped === true) return { type: 'info', text: t(lang, 'msg_check_skipped'), track: false };
    const count = Array.isArray(r.games) ? r.games.length : 0;
    const bought = Array.isArray(r.new_purchases) ? r.new_purchases.length : 0;
    let result = count ? tn(lang, 'updates_count', count) : t(lang, 'all_up_to_date');
    if (bought) result += `, ${tn(lang, 'new_purchases', bought)}`;
    return { type: 'success', text: t(lang, 'msg_check_done', { result }), track: false };
  }
  if (action === 'auto_update' || action === 'muted') {
    if (typeof r[action] === 'boolean') return null;
    return { type: 'error', text: t(lang, 'msg_rejected', { name, reason: reasonText(lang, r.reason) }), track: false };
  }
  if (r.queued === true) return { type: 'success', text: t(lang, 'msg_queued', { name }), track: true };
  if (r.sent === true) return { type: 'success', text: t(lang, SENT_KEYS[action] || 'msg_install_sent', { name }), track: action !== 'launch' };
  if (r.reason) return { type: 'error', text: t(lang, 'msg_rejected', { name, reason: reasonText(lang, r.reason) }), track: false };
  if (action === 'install' || action === 'update') {
    // Not handed over (agent not reachable or did not confirm): the job list keeps it
    return { type: 'info', text: t(lang, 'msg_pending', { name }), track: true };
  }
  return { type: 'error', text: t(lang, 'msg_rejected', { name, reason: reasonText(lang, '') }), track: false };
}

/** A fingerprint of the job state, taken when an action is sent: an end state equal to it is the
 * end of an older job (also when Home Assistant writes it again after an MQTT reconnect). */
export function statusSignature(status) {
  if (!status) return '';
  return `${status.state}|${JSON.stringify(status.attributes || {})}`;
}

/** A running job names the game (as protocol.state_appids for a running job): started lists the
 * games of the job, the other running states the one game they are about. */
export function jobRunsFor(appid, status) {
  if (!status || !jobActive(status.state, attrs(status))) return false;
  const a = attrs(status);
  return asIds(status.state === 'started' ? a.appids : a.appid).includes(String(appid));
}

/** How the job ended for one game: ok, no_space, failed, game_running, skipped, aborted, or null
 * while it has not ended (or the end does not name the game).
 * ran: a running job named the game since the action was sent. Then an end equal to the mark is
 * the end of that job (the end state has no job ID, so two jobs with the same result look alike).
 * aborted names no games, so it only counts for a game whose job was seen running. */
export function jobOutcome(appid, status, mark, ran = false) {
  if (!status) return null;
  if (!ran && statusSignature(status) === mark) return null;
  const a = attrs(status);
  if (status.state === 'aborted') return ran ? 'aborted' : null;
  if (status.state !== 'done' && status.state !== 'done_with_errors') return null;
  const id = String(appid);
  const has = (key) => asIds(a[key]).includes(id);
  if (has('ok')) return 'ok';
  if (Array.isArray(a.no_space) && a.no_space.some((k) => k && String(k.appid) === id)) return 'no_space';
  if (has('failed')) return 'failed';
  if (has('game_running')) return 'game_running';
  if (has('skipped')) return 'skipped';
  return null;
}

const OUTCOME_MESSAGES = {
  no_space: ['error', 'msg_no_space'],
  failed: ['error', 'msg_failed'],
  game_running: ['info', 'msg_game_running'],
  skipped: ['info', 'msg_skipped'],
  aborted: ['info', 'msg_aborted'],
};

/** The message when a watched job is done: an uninstalled game left the library, an install or
 * update job ended for the game, a move of the game ended (an end of the download progress sensor's
 * move_result other than the one when it was sent, tracked.mark). null while it is not done. */
export function completionMessage(lang, tracked, libraryState, status, progressState) {
  if (tracked.action === 'move') {
    const end = moveResultOf(progressState);
    if (!end || end.appid !== String(tracked.appid) || moveMark(end) === tracked.mark) {
      // Seen running (or requested) and now gone without an end: HA gave up waiting for the PC
      const now = moveInfo(progressState);
      if (tracked.seen && !(now && now.appid === String(tracked.appid))) {
        return { type: 'error', text: t(lang, 'msg_move_failed', { name: tracked.name, reason: t(lang, 'reason_unknown') }) };
      }
      return null;
    }
    if (end.ok) return { type: 'success', text: t(lang, end.warning ? 'msg_moved_left' : 'msg_moved', { name: tracked.name, drive: end.drive || '?' }) };
    return { type: 'error', text: t(lang, 'msg_move_failed', { name: tracked.name, reason: reasonText(lang, end.reason, 'move_') }) };
  }
  if (tracked.action === 'uninstall') {
    const library = libraryGames(libraryState);
    if (library && !library.some((g) => g.appid === tracked.appid)) {
      return { type: 'success', text: t(lang, 'msg_uninstalled', { name: tracked.name }) };
    }
    return null;
  }
  const outcome = jobOutcome(tracked.appid, status, tracked.mark, !!tracked.ran);
  if (!outcome) return null;
  if (outcome === 'ok') {
    return { type: 'success', text: t(lang, tracked.action === 'update' ? 'msg_updated' : 'msg_installed', { name: tracked.name }) };
  }
  const [type, key] = OUTCOME_MESSAGES[outcome];
  return { type, text: t(lang, key, { name: tracked.name }) };
}

function stored(key) {
  try {
    const value = window.localStorage.getItem(key);
    return typeof value === 'string' ? value : '';
  } catch (e) {
    return '';
  }
}

function store(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch (e) {
    // private mode or blocked storage: the choice lasts until the page is reloaded
  }
}

/** Ends a subscription: unsub is the function subscribeMessage resolved to (or nothing). It
 * may fail once the connection is closed; that ends the subscription as well. */
function dropSubscription(unsub) {
  if (typeof unsub !== 'function') return;
  try {
    Promise.resolve(unsub()).catch(() => {});
  } catch (e) {
    // the connection is gone
  }
}

function errorText(err) {
  if (!err) return '';
  if (typeof err === 'string') return err;
  return String(err.message || err.code || err);
}

// --- render layer ----------------------------------------------------------------------------
// A renderer is an object with:
//   styles             CSS of the card's shadow root
//   shell()            HTML of the card: elements .title, .body, .messages and a <dialog> (the
//                      confirmations), optionally a <dialog class="sheet"> (a game's sheet)
//   parts(view)        the body as [[key, html], ...], each html exactly one element; the
//                      controller replaces only parts whose HTML changed (a progress tick must
//                      not rebuild the buttons of a list and swallow a tap)
//   dialog(view, c)    the inside of the <dialog> for confirmContent c: elements #dialog-title and
//                      #dialog-text, buttons data-dialog="cancel" and data-dialog="ok"
//   sheet(view, d, a)  only with a sheet: its inside for gameDetail d and sheetActions a, with
//                      #sheet-title and #sheet-text and a button data-action="sheet_close"
//   message(view, msg) one message { id, type, text } with a dismiss button (data-action="dismiss")
//   cardSize(config)   the size for getCardSize
// view: { lang, locale, config, m (buildModel), tab (the selected tab), details (the PC status
// details are open), busy(key), failed(url) (an image that did not load) }.
// An element with data-morph is updated in place instead of being replaced when its HTML changes:
// its attributes are set and its children matched by data-k (or by position) and updated the same
// way, so an unchanged image is not loaded again and a progress bar keeps its transition. Only
// element children count in such an element (no text of its own).
// Controls carry data-action (and data-appid, data-name, data-size, data-pc, data-tab, data-id as
// the action needs) and data-key, which the controller uses for busy locks and to keep the focus.

function icon(name, spin) {
  if (OWN_ICONS[name]) {
    return `<svg class="se-icon${spin ? ' spin' : ''}" viewBox="0 0 24 24" aria-hidden="true" data-icon="${escapeHtml(name)}"><path fill-rule="evenodd" d="${OWN_ICONS[name]}"/></svg>`;
  }
  return `<ha-icon icon="${escapeHtml(name)}"${spin ? ' class="spin"' : ''}></ha-icon>`;
}

function busyAttr(busy) {
  return busy ? ' disabled aria-busy="true"' : '';
}

// --- style "tiles": Home Assistant tiles, chips and feature buttons ---------------------------

const TILES_STYLES = `
:host {
  display: block;
  --se-primary: var(--primary-color, #03a9f4);
  --se-on-primary: var(--text-primary-color, #fff);
  --se-success: var(--success-color, #43a047);
  --se-warning: var(--warning-color, #ffa600);
  --se-danger: var(--error-color, #db4437);
  --se-neutral: var(--secondary-text-color, #787c86);
  --se-text: var(--primary-text-color, #1c1d21);
  --se-text2: var(--secondary-text-color, #6b6f78);
  --se-card: var(--ha-card-background, var(--card-background-color, #fff));
  --se-border: var(--divider-color, rgba(0, 0, 0, 0.09));
  --se-surface: color-mix(in srgb, var(--se-text) 4%, transparent);
  --se-surface-2: color-mix(in srgb, var(--se-text) 7%, transparent);
  --se-track: color-mix(in srgb, var(--se-text) 12%, transparent);
  --se-r-lg: var(--ha-border-radius-lg, 12px);
  --se-r-xl: var(--ha-border-radius-xl, 16px);
  --se-r-pill: var(--ha-border-radius-pill, 9999px);
  --se-fast: var(--ha-animation-duration-fast, 150ms);
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
ha-card { overflow: hidden; color: var(--se-text); }
ha-icon { --mdc-icon-size: 20px; display: inline-flex; flex: none; }
.se-icon { width: var(--mdc-icon-size, 24px); height: var(--mdc-icon-size, 24px); flex: none; fill: currentColor; }
button { font: inherit; color: inherit; }
button:focus-visible { outline: 2px solid var(--se-primary); outline-offset: 2px; }
h2 { margin: 0; font: inherit; }
ul, ol { list-style: none; margin: 0; padding: 0; }
.green { --tone: var(--se-success); }
.blue { --tone: var(--se-primary); }
.amber { --tone: var(--se-warning); }
.red { --tone: var(--se-danger); }
.neutral { --tone: var(--se-neutral); }
.title {
  padding: 16px 16px 0; line-height: 1.3;
  font-size: var(--ha-card-header-font-size, 1.5rem); color: var(--ha-card-header-color, var(--se-text));
}
.body { padding: 12px 12px 14px; display: flex; flex-direction: column; gap: 14px; }
.hint { margin: 0; padding: 0 4px; font-size: 0.8125rem; line-height: 1.45; color: var(--se-text2); }

/* hero: the running game */
.hero {
  position: relative; aspect-ratio: 460 / 215; border-radius: var(--se-r-lg); overflow: hidden;
  background: #222; color: #fff;
}
.body > .hero:first-child { margin: -12px -12px 0; border-radius: 0; }
.hero img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
.hero::after {
  content: ""; position: absolute; inset: 0;
  background: linear-gradient(180deg, rgba(0, 0, 0, 0) 35%, rgba(0, 0, 0, 0.78) 100%);
}
.hero-top { position: absolute; top: 10px; inset-inline: 10px; display: flex; justify-content: space-between; gap: 8px; z-index: 1; }
.glass {
  display: inline-flex; align-items: center; gap: 6px; height: 28px; padding: 0 10px;
  border-radius: var(--se-r-pill); background: rgba(0, 0, 0, 0.45); backdrop-filter: blur(8px);
  font-size: 0.75rem; font-weight: 500; white-space: nowrap; --mdc-icon-size: 16px;
}
.dot { width: 8px; height: 8px; border-radius: 50%; background: #6ee07a; box-shadow: 0 0 0 3px rgba(110, 224, 122, 0.25); }
.hero-text { position: absolute; inset-inline: 14px; bottom: 12px; z-index: 1; }
.hero-name { font-size: 1.375rem; font-weight: 700; line-height: 1.2; overflow-wrap: anywhere; text-shadow: 0 1px 3px rgba(0, 0, 0, 0.5); }

/* chips */
.status { display: flex; flex-direction: column; gap: 12px; }
.chips { display: flex; flex-wrap: wrap; gap: 8px; }
.chip {
  display: inline-flex; align-items: center; gap: 6px; height: 32px; padding: 0 12px 0 8px;
  border-radius: var(--se-r-pill); background: var(--se-card); border: 1px solid var(--se-border);
  font-size: 0.75rem; font-weight: 500; white-space: nowrap; --mdc-icon-size: 18px;
}
.chip ha-icon { color: var(--tone); }
.chip.blue {
  background: color-mix(in srgb, var(--se-primary) 14%, transparent); border-color: transparent;
  color: color-mix(in srgb, var(--se-primary) 80%, var(--se-text));
}
.chip.blue ha-icon { color: inherit; }
.chip.neutral ha-icon { color: var(--se-text2); }

/* tiles and rows */
.tile { background: var(--se-surface); border-radius: var(--se-r-lg); padding: 10px; }
.tile.slim { padding: 4px 4px 4px 10px; }
.trow { display: flex; align-items: center; gap: 10px; min-height: 44px; }
.bub {
  position: relative; width: 40px; height: 40px; border-radius: 50%; display: grid; place-items: center;
  flex: none; color: var(--tone); --mdc-icon-size: 22px;
}
.bub::before { content: ""; position: absolute; inset: 0; border-radius: inherit; background: var(--tone); opacity: 0.18; }
.bub ha-icon, .bub .se-icon { position: relative; }
.info { flex: 1; min-width: 0; }
.p { font-size: 0.875rem; font-weight: 500; line-height: 1.4; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.p.wrap { white-space: normal; overflow-wrap: anywhere; }
.s { font-size: 0.75rem; line-height: 1.4; color: var(--se-text2); overflow-wrap: anywhere; }
.big { font-size: 1.25rem; font-weight: 700; color: var(--se-primary); font-variant-numeric: tabular-nums; white-space: nowrap; }
.bar { height: 8px; border-radius: var(--se-r-pill); background: var(--se-track); overflow: hidden; margin: 10px 0 8px; }
.bar > i { display: block; height: 100%; border-radius: inherit; background: var(--se-primary); transition: width 0.4s; }
.bar.thin { height: 6px; margin: 8px 0 2px; }
.mini { display: flex; align-items: center; gap: 6px; font-size: 0.75rem; color: var(--se-text2); --mdc-icon-size: 16px; }
.mini span { min-width: 0; overflow-wrap: anywhere; }
.tag {
  display: inline-block; height: 18px; line-height: 18px; padding: 0 7px; border-radius: var(--se-r-pill);
  font-size: 0.6875rem; font-weight: 600; white-space: nowrap;
  max-width: 100%; box-sizing: border-box; overflow: hidden; text-overflow: ellipsis; vertical-align: middle;
  background: color-mix(in srgb, var(--tone) 18%, transparent);
  color: color-mix(in srgb, var(--tone) 72%, var(--se-text));
}
.tag.neutral { color: var(--se-text2); }
.s .tag { margin-inline-end: 4px; }

/* section label */
.lab {
  display: flex; align-items: center; justify-content: space-between; gap: 8px; margin: 0 4px -6px;
  font-size: 0.8125rem; font-weight: 700; letter-spacing: 0.2px;
}
.lab .cnt { font-weight: 500; color: var(--se-text2); font-size: 0.75rem; }

/* PC */
.pc { display: flex; flex-direction: column; gap: 14px; }
.feat { display: grid; grid-template-columns: repeat(auto-fit, minmax(68px, 1fr)); gap: 8px; margin-top: 10px; }
.cb {
  position: relative; appearance: none; border: 0; cursor: pointer; min-height: 58px; padding: 6px 4px;
  border-radius: var(--se-r-lg); background: color-mix(in srgb, var(--se-neutral) 14%, transparent);
  color: var(--se-text); display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 4px; font-size: 0.6875rem; font-weight: 500; line-height: 1.2; text-align: center;
  transition: background var(--se-fast) ease-in-out, transform var(--se-fast); --mdc-icon-size: 22px;
}
.cb:hover { background: color-mix(in srgb, var(--se-neutral) 22%, transparent); }
.cb:active { transform: scale(0.97); }
.cb.danger { background: color-mix(in srgb, var(--se-danger) 12%, transparent); color: color-mix(in srgb, var(--se-danger) 80%, var(--se-text)); }
.cb.danger:hover { background: color-mix(in srgb, var(--se-danger) 20%, transparent); }
.cb.primary { background: color-mix(in srgb, var(--se-primary) 14%, transparent); color: color-mix(in srgb, var(--se-primary) 80%, var(--se-text)); }
.cb.primary:hover { background: color-mix(in srgb, var(--se-primary) 22%, transparent); }
.stats { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 8px; margin-top: 10px; }
.stat {
  display: flex; align-items: center; gap: 8px; padding: 8px 10px; min-width: 0;
  border-radius: var(--se-r-lg); background: var(--se-card); border: 1px solid var(--se-border); --mdc-icon-size: 18px;
}
.stat ha-icon { color: var(--se-text2); }
.stat dl { margin: 0; min-width: 0; }
.stat dd { margin: 0; }
.stat .k { font-size: 0.6875rem; color: var(--se-text2); line-height: 1.25; overflow-wrap: anywhere; }
.stat .v { font-size: 0.8125rem; font-weight: 500; line-height: 1.3; overflow-wrap: anywhere; }
.stat.warn {
  border-color: color-mix(in srgb, var(--se-warning) 45%, transparent);
  background: color-mix(in srgb, var(--se-warning) 8%, var(--se-card));
}
.stat.warn ha-icon, .stat.warn .v { color: color-mix(in srgb, var(--se-warning) 72%, var(--se-text)); }
.auto-off { margin-top: 6px; }
.disc {
  appearance: none; border: 0; cursor: pointer; flex: none; display: inline-flex; align-items: center; gap: 2px;
  min-height: 44px; padding: 0 6px 0 12px; border-radius: var(--se-r-pill); background: transparent;
  color: var(--se-text2); font-size: 0.75rem; font-weight: 500; --mdc-icon-size: 18px;
  transition: background var(--se-fast), color var(--se-fast);
}
.disc:hover { background: var(--se-surface-2); color: var(--se-text); }
.disc[aria-expanded="true"] { color: var(--se-text); }
.s.hold {
  display: flex; align-items: flex-start; gap: 4px; margin-top: 2px; --mdc-icon-size: 14px;
  color: color-mix(in srgb, var(--se-warning) 72%, var(--se-text));
}
.s.hold ha-icon { margin-top: 2px; }
.force {
  display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 4px 8px;
  margin-top: 6px; padding-top: 6px; border-top: 1px dashed var(--se-border);
}
.force .pill { flex: none; }
.force small { flex: 1 1 100px; font-size: 0.6875rem; color: var(--se-text2); line-height: 1.3; text-align: end; }

/* switch */
.sw {
  appearance: none; border: 0; background: none; padding: 0; cursor: pointer; flex: none;
  min-width: 52px; height: 44px; display: inline-flex; flex-direction: column; align-items: center; justify-content: center; gap: 2px;
}
.sw .track {
  position: relative; width: 44px; height: 26px; border-radius: var(--se-r-pill);
  background: var(--switch-unchecked-track-color, var(--se-track)); transition: background var(--se-fast);
}
.sw .track::after {
  content: ""; position: absolute; top: 3px; inset-inline-start: 3px; width: 20px; height: 20px; border-radius: 50%;
  background: var(--switch-unchecked-button-color, #fff); box-shadow: 0 1px 3px rgba(0, 0, 0, 0.3);
  transition: inset-inline-start var(--se-fast);
}
.sw[aria-checked="true"] .track { background: var(--switch-checked-track-color, var(--se-primary)); }
.sw[aria-checked="true"] .track::after { inset-inline-start: 21px; background: var(--switch-checked-button-color, #fff); }
.sw.small .track { width: 36px; height: 20px; }
.sw.small .track::after { top: 2px; inset-inline-start: 2px; width: 16px; height: 16px; }
.sw.small[aria-checked="true"] .track::after { inset-inline-start: 18px; }
.sw .cap { font-size: 0.625rem; line-height: 1; color: var(--se-text2); }

/* pill and icon buttons */
.pill {
  position: relative; appearance: none; cursor: pointer; display: inline-flex; align-items: center; justify-content: center;
  gap: 6px; min-height: 44px; padding: 0 16px; border-radius: var(--se-r-pill); border: 0;
  font-size: 0.8125rem; font-weight: 500; transition: filter var(--se-fast), background var(--se-fast); --mdc-icon-size: 18px;
}
.pill.fill { background: var(--se-primary); color: var(--se-on-primary); }
.pill.fill:hover { filter: brightness(1.07); }
.pill.quiet { background: color-mix(in srgb, var(--se-primary) 12%, transparent); color: color-mix(in srgb, var(--se-primary) 80%, var(--se-text)); }
.pill.quiet:hover { background: color-mix(in srgb, var(--se-primary) 20%, transparent); }
.pill.ghost-danger { background: transparent; color: var(--se-danger); padding: 0 10px; font-size: 0.75rem; }
.pill.ghost-danger:hover { background: color-mix(in srgb, var(--se-danger) 8%, transparent); }
.pill.danger { background: var(--se-danger); color: #fff; }
.pill.neutral { background: color-mix(in srgb, var(--se-neutral) 16%, transparent); color: var(--se-text); }
.pill.grow { flex: 1; }
.ib {
  position: relative; appearance: none; border: 0; cursor: pointer; width: 44px; height: 44px; min-width: 44px; padding: 0;
  border-radius: 50%; display: grid; place-items: center; flex: none; transition: background var(--se-fast);
  background: color-mix(in srgb, var(--se-primary) 12%, transparent); color: var(--se-primary);
}
.ib:hover { background: color-mix(in srgb, var(--se-primary) 22%, transparent); }
.ib.fill { background: var(--se-primary); color: var(--se-on-primary); }
.ib.fill:hover { filter: brightness(1.07); }
.ib.danger { background: color-mix(in srgb, var(--se-danger) 12%, transparent); color: var(--se-danger); }
.ib.danger:hover { background: color-mix(in srgb, var(--se-danger) 20%, transparent); }
.ib.plain { background: transparent; color: var(--se-text2); }
.ib.plain:hover { background: var(--se-surface-2); }
.pill:disabled, .ib:disabled, .cb:disabled, .sw:disabled { cursor: default; opacity: 0.55; filter: none; transform: none; }
.toolbar { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }

/* tabs */
.tabs {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(100px, 1fr)); gap: 4px; padding: 4px; border-radius: 22px; background: var(--se-surface-2);
}
.tab {
  appearance: none; border: 0; background: none; cursor: pointer; min-width: 0;
  display: inline-flex; align-items: center; justify-content: center; gap: 5px; min-height: 44px; padding: 0 10px;
  border-radius: var(--se-r-pill); font-size: 0.8125rem; font-weight: 500; color: var(--se-text2); white-space: nowrap;
  transition: background var(--se-fast), color var(--se-fast);
}
.tab:hover { color: var(--se-text); }
.tab[aria-selected="true"] { background: var(--se-card); color: var(--se-text); box-shadow: 0 1px 3px rgba(0, 0, 0, 0.12); }
.tab:focus-visible { outline-offset: -2px; }
.tab b {
  font-weight: 700; font-size: 0.6875rem; min-width: 18px; height: 18px; padding: 0 5px; border-radius: 9px;
  display: inline-grid; place-items: center; background: color-mix(in srgb, var(--se-neutral) 20%, transparent);
}
.tab b.hot { background: var(--se-primary); color: var(--se-on-primary); }
.panel { display: flex; flex-direction: column; gap: 8px; margin-top: -4px; }
.panel:focus-visible { outline: 2px solid var(--se-primary); outline-offset: 2px; border-radius: var(--se-r-lg); }

/* game rows */
.list { display: flex; flex-direction: column; gap: 6px; }
.game {
  display: flex; align-items: center; gap: 10px; min-height: 56px; padding: 6px 6px 6px 14px;
  border-radius: var(--se-r-lg); background: var(--se-surface); transition: background var(--se-fast);
}
.game:hover { background: var(--se-surface-2); }
.game.off { opacity: 0.55; }
.acts { display: flex; align-items: center; gap: 4px; flex: none; }

/* free space */
.used { font-size: 0.75rem; font-weight: 500; color: var(--se-text2); white-space: nowrap; }
.drive.nolib { opacity: 0.6; }
.usb {
  display: inline-block; vertical-align: 1px; margin-inline-start: 6px; padding: 0 5px; border-radius: 4px;
  font-size: 0.625rem; font-weight: 700; line-height: 1.5; letter-spacing: 0.03em; color: var(--se-text2);
  border: 1px solid color-mix(in srgb, var(--se-neutral) 45%, transparent);
}
.drive .bar > i { background: linear-gradient(90deg, var(--se-primary), color-mix(in srgb, var(--se-primary) 70%, transparent)); }
.drive.exp { padding: 0; transition: background var(--se-fast); }
.drive.exp:hover, .drive.exp.open { background: var(--se-surface-2); }
.dhead {
  appearance: none; display: block; width: 100%; margin: 0; padding: 10px; border: 0; border-radius: inherit;
  background: transparent; color: inherit; font: inherit; text-align: start; cursor: pointer;
}
.dhead:focus-visible { outline: 2px solid var(--se-primary); outline-offset: -2px; }
.dhead .info, .dhead .p, .dhead .s, .dhead .bar { display: block; }
.dhead .chev { flex: none; display: inline-flex; color: var(--se-text2); --mdc-icon-size: 20px; }
.dgames { margin: 0; padding: 0 10px 10px; list-style: none; display: flex; flex-direction: column; }
.dgames li {
  display: flex; align-items: baseline; gap: 10px; padding: 6px 4px; font-size: 0.8125rem; line-height: 1.4;
  border-top: 1px solid color-mix(in srgb, var(--se-neutral) 22%, transparent);
}
.dgames li.off { opacity: 0.55; }
.dgames .gn { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dgames .gs { flex: none; color: var(--se-text2); font-variant-numeric: tabular-nums; white-space: nowrap; }
.dgames .hint { padding: 6px 4px 0; }

/* messages */
.messages { padding: 0 12px; display: flex; flex-direction: column; gap: 6px; }
.messages:not(:empty) { padding-bottom: 12px; }
.message {
  display: flex; align-items: center; gap: 10px; padding: 4px 4px 4px 8px; border-radius: var(--se-r-lg);
  background: var(--se-surface-2);
}
.message .bub { width: 32px; height: 32px; --mdc-icon-size: 18px; }
.message .text { flex: 1; min-width: 0; overflow-wrap: anywhere; font-size: 0.8125rem; line-height: 1.4; }

.spin { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
/* the PC on its way (starting, shutting down, restarting) */
.pulse > ha-icon, .pulse > .se-icon { animation: pulse 1.6s ease-in-out infinite; }
@keyframes pulse { 50% { opacity: 0.35; } }
@media (prefers-reduced-motion: reduce) {
  .spin, .pulse > ha-icon, .pulse > .se-icon { animation: none; }
  .bar > i, .sw .track, .sw .track::after, .cb, .pill, .ib, .tab, .game, .disc { transition: none; }
}

/* dialog */
dialog {
  width: min(420px, calc(100vw - 32px)); max-width: calc(100vw - 32px); padding: 0; border: 0;
  border-radius: 28px; overflow: hidden; background: var(--se-card); color: var(--se-text);
  box-shadow: 0 8px 16px -2px rgba(0, 0, 0, 0.25), 0 3px 6px -1px rgba(0, 0, 0, 0.15);
}
dialog::backdrop { background: rgba(0, 0, 0, 0.5); }
dialog.fallback[open] { position: fixed; inset: 0; margin: auto; height: fit-content; z-index: 10; }
.dimg { position: relative; aspect-ratio: 460 / 215; background: var(--se-surface-2); }
.dimg img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
.dimg::after { content: ""; position: absolute; inset: 0; background: linear-gradient(180deg, rgba(0, 0, 0, 0) 55%, var(--se-card) 100%); }
.dbody { padding: 20px; }
.dimg:not([hidden]) + .dbody { padding-top: 4px; }
.dicon { width: 48px; height: 48px; margin-bottom: 8px; position: relative; z-index: 1; border-radius: 50%; background: var(--se-card); display: grid; place-items: center; }
.dimg:not([hidden]) + .dbody .dicon { margin-top: -30px; }
.dbody h2 { margin: 0 0 6px; font-size: 1.25rem; font-weight: 700; line-height: 1.3; overflow-wrap: anywhere; }
.dbody p { margin: 0 0 12px; font-size: 0.875rem; line-height: 1.5; color: var(--se-text2); }
.dwarn {
  display: flex; gap: 8px; align-items: flex-start; padding: 10px 12px; margin-bottom: 16px; border-radius: var(--se-r-lg);
  background: color-mix(in srgb, var(--se-warning) 12%, transparent);
  color: color-mix(in srgb, var(--se-warning) 72%, var(--se-text)); font-size: 0.8125rem; line-height: 1.4; --mdc-icon-size: 18px;
}
.dwarn > span { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.dacts { display: flex; flex-wrap: wrap; gap: 8px; }
.star { display: inline-flex; vertical-align: -2px; margin-inline-start: 4px; color: var(--se-primary); --mdc-icon-size: 16px; }
.dchoice { margin: 0 0 16px; padding: 0; border: 0; min-width: 0; display: flex; flex-direction: column; gap: 6px; }
.dchoice legend { padding: 0; margin-bottom: 6px; font-size: 0.8125rem; font-weight: 500; color: var(--se-text2); }
.dopt {
  display: flex; align-items: center; gap: 10px; min-height: 44px; padding: 6px 12px; border-radius: var(--se-r-lg);
  background: var(--se-surface); cursor: pointer; font-size: 0.875rem;
}
.dopt:has(input:checked) { background: color-mix(in srgb, var(--se-primary) 14%, var(--se-surface)); }
.dopt input { margin: 0; width: 18px; height: 18px; accent-color: var(--se-primary); flex: none; }
.dopt .dl { flex: 1; min-width: 0; font-weight: 500; }
/* The free space may wrap (a drive the game does not fit on has a long text): on a narrow phone it
   takes up to 60 % of the row, so neither part runs out of the row or squeezes the name to nothing */
.dopt .df { flex: 0 1 auto; min-width: 0; max-width: 60%; text-align: end; color: var(--se-text2); font-size: 0.8125rem; }
.dopt .dn { margin-left: 8px; font-weight: 400; color: var(--se-text2); font-size: 0.8125rem; }
.dopt .dm { font-weight: 400; color: var(--se-text2); }
.dopt .df.nofit { color: color-mix(in srgb, var(--se-warning) 72%, var(--se-text)); }
.dchoice .dhint { margin: 0 0 4px; font-size: 0.8125rem; color: var(--se-text2); }
`;

function bubble(tone, mdi, spin) {
  return `<span class="bub ${tone}" aria-hidden="true">${icon(mdi, spin)}</span>`;
}

/** The bubble of the PC section: the monitor, tinted and pulsing like the PC chip (pcLook). */
function pcBubble(m) {
  const look = pcLook(m);
  return bubble(`${look.tone}${look.pulse ? ' pulse' : ''}`, 'mdi:monitor');
}

function pill(view, action, label, mdi, busy, variant, attrs = '') {
  return `<button type="button" class="pill ${variant}" data-action="${action}" data-key="${action}"${attrs}${busyAttr(busy)}>
    ${icon(busy ? 'mdi:loading' : mdi, busy)}<span>${escapeHtml(label)}</span></button>`;
}

function iconButton(view, action, appid, name, label, mdi, variant = '', attrs = '') {
  const key = `${action}:${appid}`;
  const busy = view.busy(key);
  const full = t(view.lang, `${label}_for`, { name });
  return `<button type="button" class="ib ${variant}" data-action="${action}" data-appid="${escapeHtml(appid)}"
    data-name="${escapeHtml(name)}" data-key="${escapeHtml(key)}" aria-label="${escapeHtml(full)}"
    title="${escapeHtml(t(view.lang, label))}"${attrs}${busyAttr(busy)}>${icon(busy ? 'mdi:loading' : mdi, busy)}</button>`;
}

function switchButton(view, action, appid, checked, label, title, { caption = '', attrs = '', small = false } = {}) {
  const key = `${action}:${appid}`;
  return `<button type="button" class="sw${small ? ' small' : ''}" role="switch" aria-checked="${checked ? 'true' : 'false'}"
    data-action="${action}" data-appid="${escapeHtml(appid)}" data-key="${escapeHtml(key)}"${attrs}
    aria-label="${escapeHtml(label)}" title="${escapeHtml(title)}"${busyAttr(view.busy(key))}>
    <span class="track"></span>${caption ? `<span class="cap" aria-hidden="true">${escapeHtml(caption)}</span>` : ''}</button>`;
}

function tags(view, list) {
  return list.map((tag) => `<span class="tag ${tag.tone}">${escapeHtml(t(view.lang, tag.key, tag.vars))}</span>`).join('');
}

function metaLine(tagHtml, parts) {
  const text = parts.filter(Boolean).map(escapeHtml).join(' · ');
  return tagHtml || text ? `<div class="s">${tagHtml}${text}</div>` : '';
}

function hint(text) {
  return `<p class="hint">${escapeHtml(text)}</p>`;
}

/** The warnings block of a confirmation ('' without warnings). */
export function dialogWarnings(c) {
  return c.warnings.length
    ? `<div id="dialog-warning" class="dwarn">${icon('mdi:alert-outline')}<span>${c.warnings.map((w) => `<span>${escapeHtml(w)}</span>`).join('')}</span></div>`
    : '';
}

/** The media of a drive row of the install dialog after its letter (" · SSD"), '' when unknown. */
export function driveMediaText(d) {
  return d.media ? ` · ${d.media}` : '';
}

/** The right part of a drive row of the install dialog: free space and whether the game fits
 * ("1,179 GB free · fits"), the free space alone while the size is unknown. */
export function driveFreeText(d) {
  return [d.free, d.fit].filter(Boolean).join(' · ');
}

/** The class of that part: marked when the game does not fit there. */
export function driveFreeClass(d) {
  return d.fits === false ? 'df nofit' : 'df';
}

/** The badge after a drive on USB (a disk that may be unplugged). */
function usbBadge(lang) {
  return `<span class="usb" title="${escapeHtml(t(lang, 'drive_usb_title'))}">${escapeHtml(t(lang, 'drive_usb'))}</span>`;
}

/** The star after the drive that is Steam's default for new games (as in Steam's storage settings). */
function defaultStar(lang) {
  const label = escapeHtml(t(lang, 'drive_default'));
  return `<span class="star" role="img" aria-label="${label}" title="${label}">${icon('mdi:star')}</span>`;
}

// A drive with a library whose games are known: the tile is a button that opens the list of its
// games below the use (an accordion, one drive at a time), the largest game first
function driveTile(view, row, title, label) {
  const { lang, locale } = view;
  const open = view.openDrive === row.letter;
  const id = `drive-games-${row.letter}`;
  const count = tn(lang, 'drive_games', row.games.length);
  const games = !open ? '' : row.games.length
    ? `<ul class="dgames" id="${id}">${row.games.map((game) => {
      const name = game.name || gameName(lang, view.m.names, game.appid);
      return `<li${game.unavailable ? ' class="off"' : ''}><span class="gn" title="${escapeHtml(name)}">${escapeHtml(name)}</span>
        <span class="gs">${escapeHtml(formatSize(game.size_mb, locale))}</span></li>`;
    }).join('')}</ul>`
    : `<div class="dgames" id="${id}">${hint(t(lang, 'drive_games_none'))}</div>`;
  return `<div class="tile drive exp${open ? ' open' : ''}"${title}>
    <button type="button" class="dhead" data-action="drive" data-drive="${escapeHtml(row.letter)}" data-key="drive:${escapeHtml(row.letter)}"
      aria-expanded="${open ? 'true' : 'false'}" aria-controls="${id}">
      <span class="trow">${bubble(row.used >= 90 ? 'amber' : 'neutral', row.icon)}
        <span class="info"><span class="p">${label}</span><span class="s">${escapeHtml(`${row.text} · ${count}`)}</span></span>
        ${row.hasTotal ? `<span class="used">${escapeHtml(t(lang, 'used_percent', { percent: row.used }))}</span>` : ''}
        <span class="chev" aria-hidden="true">${icon(open ? 'mdi:chevron-up' : 'mdi:chevron-down')}</span></span>
      ${row.hasTotal ? `<span class="bar thin" aria-hidden="true"><i style="width:${row.used}%"></i></span>` : ''}</button>
    ${games}</div>`;
}

// No leading icon: it could not be tapped and only repeated the state, which the tags show
function gameRow(view, list, game, name, metaParts, controls, extraClass = '') {
  const state = rowState(list, game, view.m);
  return `<li class="game${extraClass}">
    <div class="info"><div class="p" title="${escapeHtml(name)}">${escapeHtml(name)}</div>
    ${metaLine(tags(view, state.tags), metaParts)}</div>
    ${controls.length ? `<div class="acts">${controls.join('')}</div>` : ''}</li>`;
}

function canAct(view) {
  return view.m.pcOn || view.config.actions_when_pc_off;
}

function rowActionsFor(view, list, game) {
  const m = view.m;
  return rowActions({ list, game, playing: m.playing, pcOn: m.pcOn, actionsWhenPcOff: view.config.actions_when_pc_off, open: m.open, move: m.move });
}

function checkButton(view) {
  const busy = view.m.checkRunning || view.busy('check');
  return pill(view, 'check', t(view.lang, busy ? 'action_checking' : 'action_check'), 'mdi:refresh', busy, 'quiet');
}

// --- PC parts shared by both styles ---

const FACT_ICONS = {
  steam: 'mdi:steam', steam_busy: 'mdi:progress-download', last_input: 'mdi:keyboard-outline', network: 'mdi:lan',
  safe: 'mdi:shield-check-outline', off_at: 'mdi:timer-outline', active_hours: 'mdi:clock-outline',
  plugin: 'mdi:puzzle-outline',
};

function factIcon(f) {
  return f.warn && f.key === 'safe' ? 'mdi:alert-outline' : FACT_ICONS[f.key];
}

/** The PC buttons but the forced shut down, as tinted buttons with icon and short label. */
function pcButtons(view, shown) {
  const lang = view.lang;
  return ['wake', 'shutdown', 'restart', 'lock', 'close_games'].filter((a) => shown[a]).map((a) => {
    const key = `pc:${a}`;
    const busy = view.busy(key) || pcActionPending(view.m, a);
    const label = t(lang, `pc_${a}`);
    const variant = a === 'wake' ? ' primary' : (a === 'lock' ? '' : ' danger');
    return `<button type="button" class="cb${variant}" data-action="pc" data-pc="${a}" data-key="${key}"
      aria-label="${escapeHtml(label)}" title="${escapeHtml(label)}"${busyAttr(busy)}>
      ${icon(busy ? 'mdi:loading' : PC_ICONS[a], busy)}<span aria-hidden="true">${escapeHtml(t(lang, `btn_${a}`))}</span></button>`;
  });
}

/** What keeps the PC from being shut down, as one line ('' when nothing does). */
function holdLine(view) {
  const hold = pcHold(view.lang, view.m);
  return hold ? `<div class="s hold">${icon('mdi:alert-outline')}<span>${escapeHtml(hold)}</span></div>` : '';
}

/** The hint while automatic work waits for the active hours ('' when nothing waits), as a calm row
 * of the status area: cls is the row of the style (tile or ljob). The manual actions stay. */
function deferredRow(view, cls) {
  const hint = deferredHint(view.lang, view.m, view.locale);
  if (!hint) return '';
  const names = hint.names ? `<div class="s">${escapeHtml(hint.names)}</div>` : '';
  const text = `<div class="p wrap">${escapeHtml(hint.text)}</div>${names}`;
  return cls === 'tile'
    ? `<div class="tile defer"><div class="trow">${bubble('neutral', 'mdi:clock-outline')}<div class="info">${text}</div></div></div>`
    : `<div class="ljob defer">${bubble('neutral', 'mdi:clock-outline')}<div class="jb">${text}</div></div>`;
}

// The status details sit behind a disclosure (closed by default, remembered in the browser); only
// what keeps the PC from being shut down stays visible, as one line under the state
function detailsToggle(view, facts) {
  if (!facts.length) return '';
  const open = !!view.details;
  return `<button type="button" class="disc" data-action="details" data-key="pc:details" aria-expanded="${open ? 'true' : 'false'}"
    aria-controls="pc-details" aria-label="${escapeHtml(t(view.lang, 'pc_details_label'))}">
    <span>${escapeHtml(t(view.lang, 'pc_details'))}</span>${icon(open ? 'mdi:chevron-up' : 'mdi:chevron-down')}</button>`;
}

function autoOffRow(view) {
  const { lang, m } = view;
  if (!m.autoOffKnown) return '';
  return `<div class="trow auto-off" data-k="ao">${bubble('blue', 'mdi:timer-cog-outline')}
    <div class="info"><div class="p" aria-hidden="true">${escapeHtml(t(lang, 'auto_off'))}</div></div>
    ${switchButton(view, 'auto_off', 'all', m.autoOffOn, t(lang, 'auto_off'), t(lang, 'auto_off'))}</div>`;
}

// The forced shut down: apart from the other buttons and less prominent, it does not wait
function forceLine(view, shown) {
  if (!shown.force_shutdown) return '';
  const busy = view.busy('pc:force_shutdown') || pcActionPending(view.m, 'force_shutdown');
  return `<div class="force" data-k="force"><button type="button" class="pill ghost-danger" data-action="pc"
    data-pc="force_shutdown" data-key="pc:force_shutdown"${busyAttr(busy)}>
    ${icon(busy ? 'mdi:loading' : PC_ICONS.force_shutdown, busy)}<span>${escapeHtml(t(view.lang, 'pc_force_shutdown'))}</span></button>
    <small>${escapeHtml(t(view.lang, 'force_shutdown_hint'))}</small></div>`;
}

const TILES = {
  styles: TILES_STYLES,

  shell() {
    return `<ha-card>
        <div class="title" hidden></div>
        <div class="body"></div>
        <div class="messages" role="status" aria-live="polite"></div>
      </ha-card>
      <dialog aria-labelledby="dialog-title" aria-describedby="dialog-text dialog-warning"></dialog>`;
  },

  cardSize(config) {
    let size = config.title ? 1 : 0;
    size += 4 * topSections(config).length;
    if (config.sections.some((s) => LIST_SECTIONS.includes(s))) size += 6;
    return Math.max(size, 2);
  },

  parts(view) {
    const { config, m } = view;
    const parts = [];
    for (const section of topSections(config)) {
      if (section === 'status') {
        if (m.current) parts.push(['hero', TILES.hero(view)]);
        parts.push(['status', TILES.status(view)]);
      } else if (section === 'pc') {
        parts.push(['pc', TILES.pc(view)]);
      }
    }
    const tabs = cardTabs(config, m);
    if (tabs.length) {
      parts.push(['tabs', tabs.length > 1 ? TILES.tabBar(view, tabs) : TILES.label(view, view.tab)]);
      parts.push([`panel:${view.tab}`, TILES.panel(view, view.tab, tabs.length > 1)]);
    }
    return parts;
  },

  hero(view) {
    const { lang, locale, m } = view;
    const since = formatSince(m.current.started, locale);
    if (!view.config.show_images) {
      return `<div class="tile now"><div class="trow">${bubble('green', 'mdi:play')}
        <div class="info"><div class="s">${escapeHtml(t(lang, 'now_playing'))}</div>
        <div class="p" title="${escapeHtml(m.current.name)}">${escapeHtml(m.current.name)}</div>
        ${since ? `<div class="s">${escapeHtml(t(lang, 'playing_since', { time: since }))}</div>` : ''}</div></div></div>`;
    }
    return `<div class="hero"><img src="${escapeHtml(m.current.picture)}" alt="" loading="lazy">
      <div class="hero-top"><span class="glass"><span class="dot"></span>${escapeHtml(t(lang, 'now_playing'))}</span>
      ${since ? `<span class="glass">${icon('mdi:timer-outline')}${escapeHtml(t(lang, 'playing_since', { time: since }))}</span>` : ''}</div>
      <div class="hero-text"><div class="hero-name">${escapeHtml(m.current.name)}</div></div></div>`;
  },

  status(view) {
    const { lang, locale, m } = view;
    const parts = [];
    const chips = statusChips(lang, m, locale).map((c) =>
      `<span class="chip ${c.tone}${c.pulse ? ' pulse' : ''}">${icon(c.icon)}${escapeHtml(c.text)}</span>`);
    parts.push(`<div class="chips">${chips.join('')}</div>`);
    const job = m.job;
    if (job.show) parts.push(TILES.job(view));
    if (m.move) parts.push(TILES.move(view));
    parts.push(deferredRow(view, 'tile'));
    if (!m.library) parts.push(hint(t(lang, 'agent_never')));
    if (checkInStatus(view.config, m)) parts.push(`<div class="toolbar">${checkButton(view)}</div>`);
    return `<div class="status">${parts.join('')}</div>`;
  },

  job(view) {
    const { lang, m } = view;
    const job = m.job;
    const lines = [];
    if (job.appid) {
      const name = gameName(lang, m.names, job.appid);
      const isUpdate = !!m.library && m.library.some((g) => g.appid === job.appid);
      const details = [];
      if (job.phase && job.phase !== 'download') details.push(`${t(lang, `phase_${job.phase}`)} ${job.phasePercent} %`);
      else if (job.phase) details.push(t(lang, 'phase_download'));
      if (job.count > 1 && job.position) details.push(t(lang, 'job_position', { position: job.position, count: job.count }));
      lines.push(`<div class="trow">${bubble('blue', 'mdi:download')}
        <div class="info"><div class="p">${escapeHtml(t(lang, isUpdate ? 'job_update' : 'job_install', { name }))}</div>
        ${details.length ? `<div class="s">${details.map(escapeHtml).join(' · ')}</div>` : ''}</div>
        <div class="big">${job.percent} %</div></div>
        <div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${job.percent}"
          aria-label="${escapeHtml(t(lang, 'progress_label', { name }))}"><i style="width:${job.percent}%"></i></div>`);
    } else {
      const text = job.active
        ? t(lang, 'job_starting')
        : (job.job.length ? t(lang, 'job_waiting', { names: job.job.map((id) => gameName(lang, m.names, id)).join(', ') }) : '');
      if (text) {
        lines.push(`<div class="trow">${bubble('blue', job.active ? 'mdi:progress-download' : 'mdi:timer-sand')}
          <div class="info"><div class="p wrap">${escapeHtml(text)}</div></div></div>`);
      }
    }
    if (job.queue.length) {
      const names = job.queue.map((id) => gameName(lang, m.names, id)).join(', ');
      lines.push(`<div class="mini">${icon('mdi:tray-full')}<span>${escapeHtml(t(lang, 'queue', { names }))}</span></div>`);
    }
    return `<div class="tile job">${lines.join('')}</div>`;
  },

  // A move to another drive, like a running job: the game, where it goes, the phase and the percent
  move(view) {
    const { lang, m } = view;
    const move = m.move;
    const name = move.name || gameName(lang, m.names, move.appid);
    const details = moveDetails(lang, move);
    return `<div class="tile job move"><div class="trow">${bubble('blue', 'mdi:folder-move-outline')}
      <div class="info"><div class="p">${escapeHtml(t(lang, 'job_move', { name }))}</div>
      ${details.length ? `<div class="s">${details.map(escapeHtml).join(' · ')}</div>` : ''}</div>
      <div class="big">${move.percent} %</div></div>
      <div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${move.percent}"
        aria-label="${escapeHtml(t(lang, 'progress_label', { name }))}"><i style="width:${move.percent}%"></i></div></div>`;
  },

  pc(view) {
    const { lang, locale, m } = view;
    const shown = pcActions(m);
    const buttons = pcButtons(view, shown);
    const facts = pcFactItems(lang, m, locale);
    const open = !!view.details;
    const inner = [];
    inner.push(`<div class="trow" data-k="hd">${pcBubble(m)}
      <div class="info"><div class="p">${escapeHtml(t(lang, pcLook(m).text))}</div>
      <div class="s">${escapeHtml(t(lang, m.agentOnline ? 'agent_online' : 'agent_offline'))}</div>
      ${holdLine(view)}</div>${detailsToggle(view, facts)}</div>`);
    // While closed only an empty target is emitted (keeps aria-controls valid): the facts change
    // often (network, last input), and rebuilding the part would swallow taps on its buttons. While
    // open the part is updated in place (data-morph, data-k): only a changed fact is replaced, the
    // buttons and the switch stay the same elements
    if (facts.length && !open) inner.push('<div class="stats" id="pc-details" data-k="st" data-morph hidden></div>');
    if (facts.length && open) {
      inner.push(`<div class="stats" id="pc-details" data-k="st" data-morph role="group" aria-label="${escapeHtml(t(lang, 'pc_details_label'))}">
        ${facts.map((f) => `<div class="stat${f.warn ? ' warn' : ''}" data-k="${escapeHtml(f.key)}">
        ${icon(factIcon(f))}
        <dl><dt class="k">${escapeHtml(f.label)}</dt><dd class="v">${escapeHtml(f.value)}</dd></dl></div>`).join('')}</div>`);
    }
    if (buttons.length) inner.push(`<div class="feat" data-k="feat">${buttons.join('')}</div>`);
    inner.push(autoOffRow(view));
    inner.push(forceLine(view, shown));
    return `<section class="pc" data-morph><h2 class="lab" data-k="lab">${escapeHtml(t(lang, 'section_pc'))}</h2>
      <div class="tile" data-k="tile" data-morph>${inner.join('')}</div></section>`;
  },

  tabBar(view, tabs, extraClass = '') {
    const lang = view.lang;
    const buttons = tabs.map((name) => {
      const selected = name === view.tab;
      const count = tabCount(name, view.m);
      const hot = name === 'updates' ? ' hot' : '';
      return `<button type="button" class="tab" role="tab" id="tab-${name}" aria-selected="${selected ? 'true' : 'false'}"
        aria-controls="panel" tabindex="${selected ? '0' : '-1'}" data-action="tab" data-tab="${name}" data-key="tab:${name}"
        title="${escapeHtml(t(lang, `section_${name}`))}">${escapeHtml(t(lang, `tab_${name}`))}${count ? `<b class="${hot.trim()}">${escapeHtml(count)}</b>` : ''}</button>`;
    });
    return `<div class="tabs${extraClass}" role="tablist" aria-label="${escapeHtml(t(lang, 'tabs_label'))}">${buttons.join('')}</div>`;
  },

  label(view, tab) {
    const count = tabCount(tab, view.m);
    return `<h2 class="lab" id="tab-${tab}"><span>${escapeHtml(t(view.lang, `section_${tab}`))}</span>
      ${count ? `<span class="cnt">${escapeHtml(count)}</span>` : ''}</h2>`;
  },

  panel(view, tab, tabbed) {
    const bodies = {
      updates: TILES.updates, installed: TILES.installed, history: TILES.history,
      not_installed: TILES.notInstalled, free_space: TILES.free,
    };
    const role = tabbed ? ' role="tabpanel" tabindex="0"' : ' role="region"';
    return `<div class="panel" id="panel"${role} aria-labelledby="tab-${tab}">${bodies[tab](view)}</div>`;
  },

  updates(view) {
    const { lang, locale, m } = view;
    const toolbar = [];
    if (countedUpdates(m).some((g) => !m.open.has(g.appid)) && canAct(view)) {
      toolbar.push(pill(view, 'install_all', t(lang, 'action_install_all'), 'mdi:download-multiple', view.busy('install_all'), 'fill grow'));
    }
    toolbar.push(checkButton(view));
    const rows = m.updates.map((game) => {
      const name = game.name || gameName(lang, m.names, game.appid);
      const actions = rowActionsFor(view, 'updates', game);
      const controls = actions.update ? [iconButton(view, 'update', game.appid, name, 'action_update', 'mdi:update')] : [];
      return gameRow(view, 'updates', game, name, [formatSize(game.size_mb, locale)], controls);
    });
    return `<div class="toolbar">${toolbar.join('')}</div><ul class="list">${rows.join('')}</ul>`;
  },

  installed(view) {
    const { lang, locale, m } = view;
    if (!m.library) return hint(t(lang, 'agent_never'));
    if (!m.library.length) return hint(t(lang, 'no_games'));
    const rows = m.library.map((game) => {
      const name = game.name || gameName(lang, m.names, game.appid);
      const actions = rowActionsFor(view, 'installed', game);
      const meta = [sizeOnDrive(lang, locale, game)];
      if (game.unavailable) {
        meta.push(t(lang, 'drive_unavailable'));
      } else {
        const date = formatDate(game.last_played, locale);
        meta.push(date ? t(lang, 'last_played', { date }) : t(lang, 'never_played'));
      }
      const controls = [];
      if (actions.autoUpdate) {
        controls.push(switchButton(view, 'auto', game.appid, m.autoUpdate.has(game.appid), t(lang, 'auto_update_for', { name }),
          t(lang, 'auto_update'), { caption: t(lang, 'badge_auto'), attrs: ` data-name="${escapeHtml(name)}"`, small: true }));
        controls.push(switchButton(view, 'mute', game.appid, !!m.muted && m.muted.has(game.appid), t(lang, 'mute_for', { name }),
          t(lang, 'mute'), { caption: t(lang, 'badge_muted'), attrs: ` data-name="${escapeHtml(name)}"`, small: true }));
      }
      if (actions.launch) controls.push(iconButton(view, 'launch', game.appid, name, 'action_launch', 'mdi:play'));
      if (actions.uninstall) {
        controls.push(iconButton(view, 'uninstall', game.appid, name, 'action_uninstall', 'mdi:delete-outline', 'danger',
          ` data-size="${escapeHtml(game.size_mb)}"`));
      }
      return gameRow(view, 'installed', game, name, meta, controls, game.unavailable ? ' off' : '');
    });
    return `<ul class="list">${rows.join('')}</ul>`;
  },

  history(view) {
    const { lang, locale, m } = view;
    if (!m.history.known) return hint(t(lang, 'history_unknown'));
    if (!m.history.entries.length) return hint(t(lang, 'history_empty'));
    const kinds = {
      installed: ['history_installed', 'green'],
      updated: ['history_updated', 'blue'],
      removed: ['history_removed', 'red'],
    };
    const sources = { ha: 'history_by_ha', steam: 'history_by_steam' };
    const rows = m.history.entries.map((entry) => {
      const name = entry.name || (entry.appid ? gameName(lang, m.names, entry.appid) : '');
      const kind = kinds[entry.kind] || [null, 'neutral'];
      const label = kind[0] ? t(lang, kind[0]) : entry.kind;
      const meta = [formatSince(entry.time, locale)];
      if (entry.kind === 'updated' && entry.oldBuild && entry.newBuild && entry.oldBuild !== entry.newBuild) {
        meta.push(t(lang, 'history_build', { old: entry.oldBuild, new: entry.newBuild }));
      }
      if (sources[entry.source]) meta.push(t(lang, sources[entry.source]));
      const kindTag = label ? `<span class="tag ${kind[1]}">${escapeHtml(label)}</span>` : '';
      return `<li class="game"><div class="info"><div class="p" title="${escapeHtml(name)}">${escapeHtml(name)}</div>
        ${metaLine(kindTag, meta)}</div></li>`;
    });
    return `<ol class="list">${rows.join('')}</ol>`;
  },

  notInstalled(view) {
    const { lang, m } = view;
    if (!m.notInstalled.known) return hint(t(lang, 'no_api_key'));
    const toolbar = m.purchasesKnown
      ? `<div class="tile slim"><div class="trow">${bubble('blue', 'mdi:cart-arrow-down')}
        <div class="info"><div class="p wrap" aria-hidden="true">${escapeHtml(t(lang, 'auto_purchases'))}</div></div>
        ${switchButton(view, 'purchases', 'all', m.purchasesOn, t(lang, 'auto_purchases'), t(lang, 'auto_purchases'))}</div></div>`
      : '';
    if (!m.notInstalled.games.length) return `${toolbar}${hint(t(lang, 'nothing_missing'))}`;
    const rows = m.notInstalled.games.map((game) => {
      const name = game.name || gameName(lang, m.names, game.appid);
      const actions = rowActionsFor(view, 'not_installed', game);
      const controls = actions.install ? [iconButton(view, 'install', game.appid, name, 'action_install', 'mdi:download')] : [];
      return gameRow(view, 'not_installed', game, name, [], controls);
    });
    return `${toolbar}<ul class="list">${rows.join('')}</ul>`;
  },

  free(view) {
    const { lang, locale, m } = view;
    // The agent's free space sensor keeps its last values while the PC is off (no data only before
    // the agent's first message)
    if (!m.free) return hint(t(lang, m.pcOn ? 'free_unknown' : 'free_pc_off'));
    const note = m.pcOn ? '' : hint(t(lang, 'free_last_known'));
    // Without a drive with a library (an old agent): the most free space alone, then any other drives
    const total = m.free.drives.length ? '' : `<div class="tile drive"><div class="trow">${bubble('neutral', 'mdi:harddisk')}
        <div class="info"><div class="p">${escapeHtml(t(lang, 'free_most', { free: formatGb(m.free.free, locale) }))}</div></div></div></div>`;
    return note + total + driveRows(lang, locale, m.free, m.library).map((row) => {
      const title = row.path && row.path !== row.label ? ` title="${escapeHtml(row.path)}"` : '';
      const label = `${escapeHtml(row.label)}${row.usb ? usbBadge(lang) : ''}${row.default ? defaultStar(lang) : ''}`;
      // A drive without a library: muted, the hint instead of the use (it holds no games)
      if (!row.library) {
        return `<div class="tile drive nolib"><div class="trow">${bubble('neutral', row.icon)}
        <div class="info"><div class="p">${label}</div><div class="s">${escapeHtml(`${row.text} · ${row.note}`)}</div></div></div></div>`;
      }
      if (row.games) return driveTile(view, row, title, label);
      return `<div class="tile drive"${title}><div class="trow">${bubble(row.used >= 90 ? 'amber' : 'neutral', row.icon)}
        <div class="info"><div class="p">${label}</div><div class="s">${escapeHtml(row.text)}</div></div>
        ${row.hasTotal ? `<span class="used">${escapeHtml(t(lang, 'used_percent', { percent: row.used }))}</span>` : ''}</div>
        ${row.hasTotal ? `<div class="bar thin" aria-hidden="true"><i style="width:${row.used}%"></i></div>` : ''}</div>`;
    }).join('');
  },

  dialog(view, c) {
    const image = c.image
      ? `<div class="dimg" data-img><img src="${escapeHtml(c.image)}" alt=""></div>`
      : '';
    // The target drive of an install: radio buttons (a native select cannot show the star). The
    // warnings (data-warnings), the hint (data-hint) and each row's free space and fit follow the
    // drive picked and the install size when it comes, without a new render (_refreshDialog)
    const drives = c.drives && c.drives.length
      ? `<fieldset class="dchoice"><legend>${escapeHtml(c.driveLabel)}</legend>
          <p class="dhint" data-hint${c.driveHint ? '' : ' hidden'}>${escapeHtml(c.driveHint || '')}</p>${c.drives.map((d) => `<label class="dopt">
          <input type="radio" name="drive" value="${escapeHtml(d.letter)}"${d.checked ? ' checked' : ''}>
          <span class="dl">${escapeHtml(d.label)}${d.media ? `<span class="dm">${escapeHtml(driveMediaText(d))}</span>` : ''}${d.usb ? usbBadge(view.lang) : ''}${d.default ? defaultStar(view.lang) : ''}${d.note ? `<span class="dn">${escapeHtml(d.note)}</span>` : ''}</span>
          <span class="${driveFreeClass(d)}">${escapeHtml(driveFreeText(d))}</span></label>`).join('')}</fieldset>`
      : '';
    return `${image}<div class="dbody">
        <div class="dicon" aria-hidden="true">${bubble(c.tone, c.icon)}</div>
        <h2 id="dialog-title">${escapeHtml(c.title)}</h2>
        <p id="dialog-text">${escapeHtml(c.text)}</p>
        <div data-warnings>${dialogWarnings(c)}</div>${drives}
        <div class="dacts">
          <button type="button" class="pill neutral grow" data-dialog="cancel">${escapeHtml(c.cancel)}</button>
          <button type="button" class="pill ${c.okTone} grow" data-dialog="ok">${icon(c.okIcon)}<span>${escapeHtml(c.ok)}</span></button>
        </div></div>`;
  },

  message(view, msg) {
    const look = { success: ['green', 'mdi:check-circle-outline'], error: ['red', 'mdi:alert-circle-outline'], info: ['blue', 'mdi:information-outline'] };
    const [tone, mdi] = look[msg.type] || look.info;
    const label = t(view.lang, 'action_dismiss');
    return `<div class="message ${msg.type}" data-id="${msg.id}">${bubble(tone, mdi)}
      <span class="text">${escapeHtml(msg.text)}</span>
      <button type="button" class="ib plain" data-action="dismiss" data-id="${msg.id}" data-key="dismiss:${msg.id}"
        aria-label="${escapeHtml(label)}" title="${escapeHtml(label)}">${icon('mdi:close')}</button></div>`;
  },
};

// --- style "library": game artwork, a grid of capsules and a sheet per game -------------------
// Shares the tokens, chips, buttons, switches, tabs, dialog and messages of the tiles style and
// adds its own parts. The game controls are in a sheet that a tap on a capsule opens.

const LIBRARY_STYLES = `${TILES_STYLES}
.body.lib { gap: 12px; }
.ph-bg { background: linear-gradient(135deg, hsl(var(--h, 210) 28% 24%), hsl(var(--h, 210) 34% 42%)); }

/* hero: the running game, or the download while no game runs */
.lhero { position: relative; aspect-ratio: 460 / 215; border-radius: var(--se-r-lg); overflow: hidden; color: #fff; }
.body > .lhero:first-child { margin: -12px -12px 0; border-radius: 0; }
.lhero img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
.lhero::after {
  content: ""; position: absolute; inset: 0;
  background: linear-gradient(180deg, rgba(10, 12, 16, 0) 25%, rgba(10, 12, 16, 0.8) 100%);
}
.lhero .cap { position: absolute; inset-inline: 14px; bottom: 12px; z-index: 1; display: flex; align-items: flex-end; gap: 10px; }
.lhero .ctext { flex: 1; min-width: 0; }
.lbl {
  display: inline-flex; align-items: center; gap: 6px; height: 22px; padding: 0 9px 0 7px; border-radius: var(--se-r-pill);
  background: rgba(0, 0, 0, 0.42); backdrop-filter: blur(6px); font-size: 0.6875rem; font-weight: 700;
  letter-spacing: 0.5px; text-transform: uppercase; --mdc-icon-size: 14px;
}
.lbl .dot { width: 7px; height: 7px; box-shadow: none; }
.lhero .nm { margin-top: 6px; font-size: 1.375rem; font-weight: 700; line-height: 1.15; overflow-wrap: anywhere; text-shadow: 0 1px 3px rgba(0, 0, 0, 0.5); }
.lhero .mt { font-size: 0.75rem; opacity: 0.88; }
.lhero .pct { flex: none; font-size: 1.375rem; font-weight: 700; font-variant-numeric: tabular-nums; text-shadow: 0 1px 3px rgba(0, 0, 0, 0.5); }
.lhero .bar { position: absolute; inset-inline: 0; bottom: 0; height: 4px; margin: 0; border-radius: 0; z-index: 1; background: rgba(255, 255, 255, 0.25); }
.lhero .bar > i { border-radius: 0; }

/* job row */
.ljob { display: flex; align-items: center; gap: 10px; padding: 10px; border-radius: var(--se-r-lg); background: var(--se-surface); }
.ljob .thumb { position: relative; width: 92px; aspect-ratio: 231 / 87; border-radius: 8px; overflow: hidden; flex: none; }
.ljob .thumb img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
.ljob .jb { flex: 1; min-width: 0; }
.ljob .r1 { display: flex; align-items: baseline; gap: 6px; }
.ljob .r1 .p { flex: 1; min-width: 0; }
.ljob .pct { flex: none; font-weight: 700; color: var(--se-primary); font-variant-numeric: tabular-nums; }
.ljob .bar { margin: 6px 0 4px; height: 6px; }
.queue { margin: 0 4px; }

/* PC bar */
.lpc { background: var(--se-surface); border-radius: var(--se-r-lg); padding: 6px 6px 8px 10px; display: flex; flex-direction: column; gap: 8px; }
.lpc .trow { min-height: 44px; }
.lpc .bub { width: 36px; height: 36px; --mdc-icon-size: 20px; }
.lpc .p .s { display: inline; font-weight: 400; }
.facts { display: flex; flex-wrap: wrap; gap: 6px; padding-inline-end: 4px; }
.fact {
  display: inline-flex; align-items: center; gap: 6px; min-height: 32px; padding: 4px 11px 4px 9px; border-radius: var(--se-r-pill);
  background: var(--se-card); border: 1px solid var(--se-border); font-size: 0.75rem; font-weight: 500; line-height: 1.3; --mdc-icon-size: 16px;
}
.fact ha-icon { color: var(--se-text2); }
.fact small { font-size: inherit; font-weight: 400; color: var(--se-text2); }
.fact.warn { background: color-mix(in srgb, var(--se-warning) 12%, var(--se-card)); border-color: transparent; color: color-mix(in srgb, var(--se-warning) 72%, var(--se-text)); }
.fact.warn ha-icon, .fact.warn small { color: inherit; }
.lpc .feat { grid-template-columns: repeat(auto-fit, minmax(64px, 1fr)); margin: 0 4px 0 0; }
.lpc .cb { min-height: 52px; background: var(--se-card); }
.lpc .cb:hover { background: var(--se-surface-2); }
.lpc .cb.danger { background: color-mix(in srgb, var(--se-danger) 10%, var(--se-card)); }
.lpc .cb.primary { background: color-mix(in srgb, var(--se-primary) 14%, var(--se-card)); }
.lpc .auto-off { margin: 0; padding-inline-end: 4px; }
.lpc .force { margin: 0 4px 0 0; padding-top: 4px; }

/* segments instead of tabs */
.tabs.seg { display: flex; gap: 6px; padding: 2px; border-radius: 0; background: none; overflow-x: auto; scrollbar-width: none; }
.tabs.seg::-webkit-scrollbar { display: none; }
.tabs.seg .tab { flex: none; padding: 0 14px; border: 1px solid var(--se-border); }
.tabs.seg .tab[aria-selected="true"] {
  background: color-mix(in srgb, var(--se-primary) 14%, transparent); border-color: transparent; box-shadow: none;
  color: color-mix(in srgb, var(--se-primary) 80%, var(--se-text));
}
.lib .panel { margin-top: -2px; gap: 10px; }

/* the grid of capsules */
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 10px; }
.ct {
  appearance: none; border: 0; padding: 0; margin: 0; width: 100%; height: 100%; cursor: pointer; text-align: start;
  display: flex; flex-direction: column; border-radius: var(--se-r-lg); overflow: hidden; background: var(--se-surface);
  transition: transform var(--se-fast), background var(--se-fast);
}
.ct:hover { background: var(--se-surface-2); }
.ct:active { transform: scale(0.98); }
.ct.upd { box-shadow: inset 0 0 0 2px color-mix(in srgb, var(--se-warning) 60%, transparent); }
.ct.off { opacity: 0.55; }
.ct.off img { filter: grayscale(1); }
.art { position: relative; display: block; aspect-ratio: 231 / 87; overflow: hidden; flex: none; }
.art img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; display: block; }
.ph {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; padding: 6px 10px;
  color: #fff; text-align: center; font-size: 0.8125rem; font-weight: 700; line-height: 1.2;
}
.ph > span { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; overflow-wrap: anywhere; }
.flags { position: absolute; inset-inline-start: 6px; bottom: 6px; inset-inline-end: 6px; z-index: 1; display: flex; flex-wrap: wrap; gap: 4px; }
.flag {
  display: inline-flex; align-items: center; gap: 3px; height: 20px; padding: 0 7px 0 5px; border-radius: var(--se-r-pill);
  max-width: 100%; overflow: hidden; white-space: nowrap; font-size: 0.6875rem; font-weight: 700; line-height: 1;
  background: rgba(15, 17, 20, 0.68); color: #fff; backdrop-filter: blur(6px); --mdc-icon-size: 13px;
}
.flag > span { overflow: hidden; text-overflow: ellipsis; }
.flag.green { background: var(--se-success); }
.flag.blue { background: var(--se-primary); color: var(--se-on-primary); }
.flag.amber { background: var(--se-warning); color: #1c1d21; }
.ti { display: block; padding: 6px 10px 8px; min-width: 0; }
.ti .nm { display: block; font-size: 0.8125rem; font-weight: 600; line-height: 1.35; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
/* tile without a picture: the badges sit in the line below the placeholder */
.ti > .flags { position: static; margin-bottom: 4px; }
.ti .flag:not(.green):not(.blue):not(.amber) { background: var(--se-surface-2); color: var(--se-text); backdrop-filter: none; }
.ti .mt { display: flex; flex-wrap: wrap; column-gap: 8px; font-size: 0.6875rem; line-height: 1.45; color: var(--se-text2); }
.ti .mt > span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%; }
.au {
  position: absolute; top: 6px; inset-inline-end: 6px; z-index: 1; width: 24px; height: 24px; border-radius: 50%;
  display: grid; place-items: center; background: rgba(15, 17, 20, 0.62); color: #fff; backdrop-filter: blur(6px); --mdc-icon-size: 15px;
}

/* the sheet of a game: from the bottom on a phone, centred on a wide screen */
dialog.sheet {
  width: 100%; max-width: 100%; max-height: min(92vh, 92dvh); margin: auto 0 0; overflow-y: auto; overscroll-behavior: contain;
  border-radius: 28px 28px 0 0; padding-bottom: env(safe-area-inset-bottom, 0px);
}
dialog.sheet[open] { animation: sheet-in 220ms ease-out; }
dialog.sheet.fallback[open] { position: fixed; inset: auto 0 0 0; margin: 0; z-index: 9; }
@keyframes sheet-in { from { transform: translateY(32px); opacity: 0; } }
@media (min-width: 600px) {
  dialog.sheet { width: min(480px, calc(100vw - 32px)); margin: auto; border-radius: 28px; }
  dialog.sheet.fallback[open] { inset: 0; margin: auto; height: fit-content; }
  .grab { display: none; }
}
.sh { position: relative; padding: 0 16px 18px; display: flex; flex-direction: column; }
.grab { width: 36px; height: 4px; border-radius: var(--se-r-pill); background: var(--se-track); margin: 10px auto 2px; flex: none; }
.sclose { position: absolute; top: 8px; inset-inline-end: 8px; z-index: 2; }
.simg { position: relative; aspect-ratio: 460 / 215; margin: 10px 0 0; border-radius: var(--se-r-lg); overflow: hidden; }
.simg img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
.sh.img .sclose { top: 22px; inset-inline-end: 22px; background: rgba(0, 0, 0, 0.45); color: #fff; }
.sh.img .sclose:hover { background: rgba(0, 0, 0, 0.6); }
.shd { padding: 12px 0 0; }
.sh:not(.img) .shd { padding-inline-end: 48px; }
.shn { display: flex; align-items: baseline; gap: 4px 12px; margin: 0 0 4px; }
.shd h2 { flex: 1 1 auto; min-width: 0; margin: 0; font-size: 1.25rem; font-weight: 700; line-height: 1.3; overflow-wrap: anywhere; }
.gsize { flex: none; font-size: 0.875rem; color: var(--se-text2); white-space: nowrap; font-variant-numeric: tabular-nums; }
.ssub { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; font-size: 0.8125rem; color: var(--se-text2); line-height: 1.5; }
.ssub .tag { display: inline-flex; align-items: center; gap: 3px; height: 20px; line-height: 20px; --mdc-icon-size: 13px; }
.sacts { display: flex; flex-direction: column; gap: 8px; margin-top: 16px; }
.sacts .two { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 8px; }
.sacts .pill { min-height: 48px; font-size: 0.875rem; }
.sacts .ltog { padding: 4px 4px 4px 10px; border-radius: var(--se-r-lg); background: var(--se-surface); min-height: 56px; }
.sacts .ltog .bub { width: 36px; height: 36px; --mdc-icon-size: 20px; }
.pill.danger-quiet { width: 100%; background: color-mix(in srgb, var(--se-danger) 11%, transparent); color: var(--se-danger); }
.pill.danger-quiet:hover { background: color-mix(in srgb, var(--se-danger) 18%, transparent); }
.sacts .hint { padding: 0 2px; }

@media (prefers-reduced-motion: reduce) {
  dialog.sheet[open] { animation: none; }
  .ct { transition: none; }
  .ct:active { transform: none; }
}
`;

/** The tinted placeholder style of a game (its hue from the app ID). */
function hueStyle(appid) {
  return ` style="--h:${placeholderHue(appid)}"`;
}

/** An image of a game, or nothing when images are off or this one did not load. */
function gameImage(view, url, extra = '') {
  if (!view.config.show_images || view.failed(url)) return '';
  return `<img data-k="img" src="${escapeHtml(url)}" alt="" loading="lazy" decoding="async"${extra}>`;
}

function libFlag(f) {
  return `<span class="flag ${f.tone}">${f.icon ? icon(f.icon) : ''}<span>${escapeHtml(f.text)}</span></span>`;
}

function progressBar(view, name, percent) {
  return `<div class="bar" data-k="bar" data-morph role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}"
    aria-label="${escapeHtml(t(view.lang, 'progress_label', { name }))}"><i data-morph style="width:${percent}%"></i></div>`;
}

/** The phase of the running job and the position in a job of several games, as texts. */
function jobDetails(lang, job) {
  const details = [];
  if (job.phase && job.phase !== 'download') details.push(`${t(lang, `phase_${job.phase}`)} ${job.phasePercent} %`);
  else if (job.phase) details.push(t(lang, 'phase_download'));
  if (job.count > 1 && job.position) details.push(t(lang, 'job_position', { position: job.position, count: job.count }));
  return details;
}

function queueLine(view) {
  const { lang, m } = view;
  if (!m.job.queue.length) return '';
  const names = m.job.queue.map((id) => gameName(lang, m.names, id)).join(', ');
  return `<div class="mini queue">${icon('mdi:tray-full')}<span>${escapeHtml(t(lang, 'queue', { names }))}</span></div>`;
}

const LIBRARY = {
  styles: LIBRARY_STYLES,

  shell() {
    return `<ha-card>
        <div class="title" hidden></div>
        <div class="body lib"></div>
        <div class="messages" role="status" aria-live="polite"></div>
      </ha-card>
      <dialog class="sheet" aria-labelledby="sheet-title" aria-describedby="sheet-text"></dialog>
      <dialog aria-labelledby="dialog-title" aria-describedby="dialog-text dialog-warning"></dialog>`;
  },

  cardSize(config) {
    const top = topSections(config);
    let size = config.title ? 1 : 0;
    if (top.includes('status')) size += 5;
    if (top.includes('pc')) size += 3;
    if (config.sections.some((s) => LIST_SECTIONS.includes(s))) size += 8;
    return Math.max(size, 2);
  },

  parts(view) {
    const { config, m } = view;
    const top = topSections(config);
    const parts = [];
    // The hero shows the running game; while none runs, the game the job downloads
    const heroJob = !m.current && !!m.job.appid;
    for (const section of top) {
      if (section === 'status') {
        if (m.current || heroJob) parts.push(['hero', LIBRARY.hero(view, heroJob)]);
        if (m.job.show && !heroJob) parts.push(['job', LIBRARY.job(view)]);
        if (m.move) parts.push(['move', LIBRARY.move(view)]);
        parts.push(['status', LIBRARY.status(view, top.includes('pc'), heroJob)]);
      } else if (section === 'pc') {
        parts.push(['pc', LIBRARY.pc(view)]);
      }
    }
    const tabs = cardTabs(config, m);
    if (tabs.length) {
      parts.push(['tabs', tabs.length > 1 ? TILES.tabBar(view, tabs, ' seg') : TILES.label(view, view.tab)]);
      parts.push([`panel:${view.tab}`, LIBRARY.panel(view, view.tab, tabs.length > 1)]);
    }
    return parts;
  },

  hero(view, heroJob) {
    const { lang, locale, m } = view;
    if (!heroJob) {
      const game = m.current;
      const since = formatSince(game.started, locale);
      return `<div class="lhero ph-bg" data-morph${hueStyle(game.appid)}>${gameImage(view, game.picture)}
        <div class="cap" data-k="cap"><div class="ctext"><span class="lbl"><span class="dot"></span>${escapeHtml(t(lang, 'now_playing'))}</span>
        <div class="nm">${escapeHtml(game.name)}</div>
        ${since ? `<div class="mt">${escapeHtml(t(lang, 'playing_since', { time: since }))}</div>` : ''}</div></div></div>`;
    }
    const job = m.job;
    const name = gameName(lang, m.names, job.appid);
    const isUpdate = !!m.library && m.library.some((g) => g.appid === job.appid);
    const details = jobDetails(lang, job);
    // The texts change with the phase, the bar with every tick: both are updated in place
    return `<div class="lhero ph-bg" data-morph${hueStyle(job.appid)}>${gameImage(view, steamImageUrl(m.images, job.appid, 'header'))}
      <div class="cap" data-k="cap"><div class="ctext"><span class="lbl">${icon('mdi:download')}${escapeHtml(t(lang, isUpdate ? 'tag_update' : 'action_install'))}</span>
      <div class="nm">${escapeHtml(name)}</div>
      ${details.length ? `<div class="mt">${details.map(escapeHtml).join(' · ')}</div>` : ''}</div>
      <div class="pct" aria-hidden="true">${job.percent} %</div></div>
      ${progressBar(view, name, job.percent)}</div>`;
  },

  job(view) {
    const { lang, m } = view;
    const job = m.job;
    if (job.appid) {
      const name = gameName(lang, m.names, job.appid);
      const isUpdate = !!m.library && m.library.some((g) => g.appid === job.appid);
      const details = jobDetails(lang, job);
      return `<div class="ljob" data-morph>
        <span class="thumb ph-bg" data-k="th" data-morph${hueStyle(job.appid)} aria-hidden="true">${gameImage(view, steamImageUrl(m.images, job.appid, 'capsule'))}</span>
        <div class="jb" data-k="jb" data-morph>
          <div class="r1" data-k="r1"><div class="p">${escapeHtml(t(lang, isUpdate ? 'job_update' : 'job_install', { name }))}</div>
          <span class="pct">${job.percent} %</span></div>
          ${progressBar(view, name, job.percent)}
          ${details.length ? `<div class="s" data-k="d">${details.map(escapeHtml).join(' · ')}</div>` : ''}
          ${job.queue.length ? `<div data-k="q">${queueLine(view)}</div>` : ''}</div></div>`;
    }
    const text = job.active
      ? t(lang, 'job_starting')
      : (job.job.length ? t(lang, 'job_waiting', { names: job.job.map((id) => gameName(lang, m.names, id)).join(', ') }) : '');
    if (!text && !job.queue.length) return '';
    return `<div class="ljob">${text ? `${bubble('blue', job.active ? 'mdi:progress-download' : 'mdi:timer-sand')}
      <div class="jb"><div class="p wrap">${escapeHtml(text)}</div>${queueLine(view)}</div>` : `<div class="jb">${queueLine(view)}</div>`}</div>`;
  },

  // A move to another drive, as the job row: its capsule, name, percent, bar, route and phase,
  // updated in place on each tick
  move(view) {
    const { lang, m } = view;
    const move = m.move;
    const name = move.name || gameName(lang, m.names, move.appid);
    const details = moveDetails(lang, move);
    return `<div class="ljob" data-morph>
      <span class="thumb ph-bg" data-k="th" data-morph${hueStyle(move.appid)} aria-hidden="true">${gameImage(view, steamImageUrl(m.images, move.appid, 'capsule'))}</span>
      <div class="jb" data-k="jb" data-morph>
        <div class="r1" data-k="r1"><div class="p">${escapeHtml(t(lang, 'job_move', { name }))}</div>
        <span class="pct">${move.percent} %</span></div>
        ${progressBar(view, name, move.percent)}
        ${details.length ? `<div class="s" data-k="d">${details.map(escapeHtml).join(' · ')}</div>` : ''}</div></div>`;
  },

  status(view, pcShown, heroJob) {
    const { lang, locale, m } = view;
    // The PC bar says on or off; the job's percent is in the hero or the job row, the move's in its row
    const chips = statusChips(lang, m, locale)
      .filter((c) => !(c.key === 'pc' && pcShown) && !(c.key === 'download' && m.job.appid) && c.key !== 'move')
      .map((c) => `<span class="chip ${c.tone}${c.pulse ? ' pulse' : ''}">${icon(c.icon)}${escapeHtml(c.text)}</span>`);
    const parts = [];
    if (chips.length) parts.push(`<div class="chips">${chips.join('')}</div>`);
    const queue = heroJob ? queueLine(view) : '';
    if (queue) parts.push(queue);
    const waiting = deferredRow(view, 'ljob');
    if (waiting) parts.push(waiting);
    if (!m.library) parts.push(hint(t(lang, 'agent_never')));
    if (checkInStatus(view.config, m)) parts.push(`<div class="toolbar">${checkButton(view)}</div>`);
    return parts.length ? `<div class="status">${parts.join('')}</div>` : '';
  },

  pc(view) {
    const { lang, locale, m } = view;
    const shown = pcActions(m);
    const facts = pcFactItems(lang, m, locale);
    const open = !!view.details;
    const inner = [];
    inner.push(`<div class="trow" data-k="hd">${pcBubble(m)}
      <div class="info"><div class="p">${escapeHtml(t(lang, pcLook(m).text))}
      <span class="s">· ${escapeHtml(t(lang, m.agentOnline ? 'agent_online' : 'agent_offline'))}</span></div>
      ${holdLine(view)}</div>${detailsToggle(view, facts)}</div>`);
    // Closed: an empty target only; open: updated in place (see the tiles style)
    if (facts.length && !open) inner.push('<div class="facts" id="pc-details" data-k="st" data-morph hidden></div>');
    if (facts.length && open) {
      inner.push(`<div class="facts" id="pc-details" data-k="st" data-morph role="group" aria-label="${escapeHtml(t(lang, 'pc_details_label'))}">
        ${facts.map((f) => `<span class="fact${f.warn ? ' warn' : ''}" data-k="${escapeHtml(f.key)}">${icon(factIcon(f))}
        <span><small>${escapeHtml(f.label)}</small> ${escapeHtml(f.value)}</span></span>`).join('')}</div>`);
    }
    const buttons = pcButtons(view, shown);
    if (buttons.length) inner.push(`<div class="feat" data-k="feat">${buttons.join('')}</div>`);
    inner.push(autoOffRow(view));
    inner.push(forceLine(view, shown));
    return `<section class="lpc" data-morph aria-label="${escapeHtml(t(lang, 'section_pc'))}">${inner.join('')}</section>`;
  },

  panel(view, tab, tabbed) {
    const bodies = {
      updates: LIBRARY.updates, installed: LIBRARY.installed, not_installed: LIBRARY.notInstalled,
      history: TILES.history, free_space: TILES.free,
    };
    const role = tabbed ? ' role="tabpanel" tabindex="0"' : ' role="region"';
    return `<div class="panel" id="panel" data-morph${role} aria-labelledby="tab-${tab}">${bodies[tab](view)}</div>`;
  },

  /** One capsule: a button that opens the game's sheet. The placeholder with the name is under the
   * image, so a game without a picture (images off, none on the CDN) still has its tile. */
  tile(view, list, game) {
    const lang = view.lang;
    const g = gridTile(lang, view.locale, list, game, view.m);
    const img = gameImage(view, steamImageUrl(view.m.images, g.appid, 'capsule'));
    const flags = g.flags.slice(0, 2).map(libFlag).join('');
    const metaHtml = g.meta.length ? `<span class="mt">${g.meta.map((text) => `<span>${escapeHtml(text)}</span>`).join('')}</span>` : '';
    // Automatic updates: a small mark in the corner of the image (the label says it in words)
    const auto = g.auto ? `<span class="au" data-k="a" title="${escapeHtml(t(lang, 'auto_update'))}">${icon('mdi:autorenew')}</span>` : '';
    const flagsHtml = flags ? `<span class="flags" data-k="f" aria-hidden="true">${flags}</span>` : '';
    // Without a picture the placeholder shows the name, so the line below does not repeat it; the
    // state badges move into that line, over the placeholder they would cover the name
    const info = img ? `<span class="nm">${escapeHtml(g.name)}</span>${metaHtml}` : `${flagsHtml}${metaHtml}`;
    return `<li data-k="${escapeHtml(g.appid)}" data-morph><button type="button" class="ct${g.off ? ' off' : ''}${g.update ? ' upd' : ''}"
      data-morph data-action="sheet" data-list="${list}" data-appid="${escapeHtml(g.appid)}" data-name="${escapeHtml(g.name)}"
      data-key="sheet:${list}:${escapeHtml(g.appid)}" aria-haspopup="dialog" aria-label="${escapeHtml(g.label)}">
      <span class="art ph-bg" data-k="art" data-morph${hueStyle(g.appid)}><span class="ph" data-k="ph" aria-hidden="true"><span>${escapeHtml(g.name)}</span></span>${img}${auto}${img ? flagsHtml : ''}</span>
      ${info ? `<span class="ti" data-k="ti" aria-hidden="true">${info}</span>` : ''}</button></li>`;
  },

  grid(view, list, games) {
    return `<ul class="grid" data-k="grid" data-morph>${games.map((game) => LIBRARY.tile(view, list, game)).join('')}</ul>`;
  },

  updates(view) {
    const { lang, m } = view;
    const toolbar = [];
    if (countedUpdates(m).some((g) => !m.open.has(g.appid)) && canAct(view)) {
      toolbar.push(pill(view, 'install_all', t(lang, 'action_install_all'), 'mdi:download-multiple', view.busy('install_all'), 'fill grow'));
    }
    toolbar.push(checkButton(view));
    return `<div class="toolbar" data-k="tb">${toolbar.join('')}</div>${LIBRARY.grid(view, 'updates', m.updates)}`;
  },

  installed(view) {
    const { lang, m } = view;
    if (!m.library) return hint(t(lang, 'agent_never'));
    if (!m.library.length) return hint(t(lang, 'no_games'));
    return LIBRARY.grid(view, 'installed', m.library);
  },

  notInstalled(view) {
    const { lang, m } = view;
    if (!m.notInstalled.known) return hint(t(lang, 'no_api_key'));
    const toolbar = m.purchasesKnown
      ? `<div class="tile slim" data-k="pur"><div class="trow">${bubble('blue', 'mdi:cart-arrow-down')}
        <div class="info"><div class="p wrap" aria-hidden="true">${escapeHtml(t(lang, 'auto_purchases'))}</div></div>
        ${switchButton(view, 'purchases', 'all', m.purchasesOn, t(lang, 'auto_purchases'), t(lang, 'auto_purchases'))}</div></div>`
      : '';
    if (!m.notInstalled.games.length) return `${toolbar}${hint(t(lang, 'nothing_missing'))}`;
    return `${toolbar}${LIBRARY.grid(view, 'not_installed', m.notInstalled.games)}`;
  },

  sheet(view, d, actions) {
    const lang = view.lang;
    const close = t(lang, 'action_close');
    const img = gameImage(view, steamImageUrl(view.m && view.m.images, d.appid, 'header'));
    const button = (action, variant, mdi, labelKey, extra = '') => {
      const key = `${action}:${d.appid}`;
      const busy = view.busy(key);
      return `<button type="button" class="pill ${variant}" data-action="${action}" data-appid="${escapeHtml(d.appid)}"
        data-name="${escapeHtml(d.name)}" data-key="${escapeHtml(key)}" data-k="${action}"${extra}${busyAttr(busy)}>
        ${icon(busy ? 'mdi:loading' : mdi, busy)}<span>${escapeHtml(t(lang, labelKey))}</span></button>`;
    };
    const primary = [];
    if (actions.includes('launch')) primary.push(button('launch', 'fill', 'mdi:play', 'action_launch'));
    if (actions.includes('update')) primary.push(button('update', actions.includes('launch') ? 'quiet' : 'fill', 'mdi:update', 'action_update'));
    if (actions.includes('install')) primary.push(button('install', 'fill', 'mdi:download', 'action_install'));
    const rows = [];
    if (primary.length) rows.push(`<div class="two" data-k="two" data-morph>${primary.join('')}</div>`);
    if (actions.includes('auto')) {
      rows.push(`<div class="trow ltog" data-k="auto" data-morph>${bubble('blue', 'mdi:autorenew')}
        <div class="info" data-k="i"><div class="p wrap" aria-hidden="true">${escapeHtml(t(lang, 'auto_update'))}</div></div>
        ${switchButton(view, 'auto', d.appid, d.auto, t(lang, 'auto_update_for', { name: d.name }), t(lang, 'auto_update'),
          { attrs: ` data-name="${escapeHtml(d.name)}" data-k="sw" data-morph` })}</div>`);
      rows.push(`<div class="trow ltog" data-k="mute" data-morph>${bubble('neutral', 'mdi:bell-off-outline')}
        <div class="info" data-k="i"><div class="p wrap" aria-hidden="true">${escapeHtml(t(lang, 'mute'))}</div></div>
        ${switchButton(view, 'mute', d.appid, d.muted, t(lang, 'mute_for', { name: d.name }), t(lang, 'mute'),
          { attrs: ` data-name="${escapeHtml(d.name)}" data-k="sw" data-morph` })}</div>`);
    }
    // Move and Uninstall side by side (one of them alone takes the whole width)
    const manage = [];
    if (actions.includes('move')) manage.push(button('move', 'quiet', 'mdi:folder-move-outline', 'action_move'));
    if (actions.includes('uninstall')) {
      const size = d.installed ? d.installed.size_mb : 0;
      manage.push(button('uninstall', 'danger-quiet', 'mdi:delete-outline', 'action_uninstall', ` data-size="${escapeHtml(size)}"`));
    }
    if (manage.length) rows.push(`<div class="two" data-k="manage" data-morph>${manage.join('')}</div>`);
    // Waiting for the active hours: say until when (Update or Install above starts it at once)
    const waiting = d.deferred ? deferredHint(lang, view.m, view.locale) : null;
    if (waiting) rows.push(`<div data-k="defer">${hint(waiting.text)}</div>`);
    // Only the switch (or nothing) while the PC is off and actions_when_pc_off is off: say why
    if (!canAct(view) && !d.unavailable && !d.playing && (d.installed || d.update || d.owned)) {
      rows.push(`<div data-k="hint">${hint(t(lang, 'sheet_pc_off'))}</div>`);
    }
    const tagsHtml = sheetFlags(lang, d).map((f) => `<span class="tag ${f.tone}">${f.icon ? icon(f.icon) : ''}${escapeHtml(f.text)}</span>`).join('');
    const meta = sheetMeta(lang, view.locale, d).map((text) => `<span>${escapeHtml(text)}</span>`).join('<span aria-hidden="true">·</span>');
    // The install size of a game that is not installed, right of the name
    const size = wantsGameSize(d) && view.gameSize ? gameSizeText(view.gameSize(d.appid), view.locale) : '';
    const sizeHtml = size
      ? `<span class="gsize" data-k="size" title="${escapeHtml(t(lang, 'install_size'))}">${escapeHtml(size)}</span>`
      : '';
    return `<div class="sh${img ? ' img' : ''}" data-morph>
      <div class="grab" data-k="grab" aria-hidden="true"></div>
      ${img ? `<div class="simg ph-bg" data-k="img" data-morph${hueStyle(d.appid)}>${img}</div>` : ''}
      <button type="button" class="ib plain sclose" data-k="x" data-action="sheet_close" data-key="sheet:close"
        aria-label="${escapeHtml(close)}" title="${escapeHtml(close)}">${icon('mdi:close')}</button>
      <div class="shd" data-k="hd"><div class="shn"><h2 id="sheet-title">${escapeHtml(d.name)}</h2>${sizeHtml}</div>
      <div class="ssub" id="sheet-text">${tagsHtml}${meta}</div></div>
      <div class="sacts" data-k="acts" data-morph>${rows.join('')}</div></div>`;
  },

  dialog: TILES.dialog,
  message: TILES.message,
};

/** The styles the card can render, chosen by the option style (normalizeStyle). */
export const RENDERERS = { tiles: TILES, library: LIBRARY };

function rendererFor(config) {
  return RENDERERS[config.style] || RENDERERS[STYLES[0]];
}

// --- updating the DOM in place ----------------------------------------------------------------
// The HTML each element was built from: comparing it (not the live outerHTML, which elements of
// Home Assistant such as ha-icon may change) tells whether the element has to change.
const SOURCE = new WeakMap();

/** One element from HTML, with the source of it and every element in it remembered. */
function htmlElement(html) {
  const template = document.createElement('template');
  template.innerHTML = html.trim();
  const element = template.content.firstElementChild;
  for (const el of [element, ...element.querySelectorAll('*')]) SOURCE.set(el, el.outerHTML);
  return element;
}

/** Brings the element old in the page to the state of next (from htmlElement) and returns the
 * element that is in the page afterwards: old itself when it was unchanged or could be updated in
 * place (both carry data-morph), next otherwise. */
function morphElement(old, next) {
  const source = SOURCE.get(next);
  if ((SOURCE.has(old) ? SOURCE.get(old) : old.outerHTML) === source) return old;
  if (old.tagName !== next.tagName || !old.hasAttribute('data-morph') || !next.hasAttribute('data-morph')) {
    old.replaceWith(next);
    return next;
  }
  for (const attr of [...old.attributes]) if (!next.hasAttribute(attr.name)) old.removeAttribute(attr.name);
  for (const attr of [...next.attributes]) if (old.getAttribute(attr.name) !== attr.value) old.setAttribute(attr.name, attr.value);
  const keyOf = (el, i) => (el.dataset.k !== undefined ? `k:${el.dataset.k}` : `i:${i}`);
  const olds = new Map([...old.children].map((el, i) => [keyOf(el, i), el]));
  const children = [...next.children].map((child, i) => {
    const match = olds.get(keyOf(child, i));
    if (!match) return child;
    olds.delete(keyOf(child, i));
    return morphElement(match, child);
  });
  for (const el of olds.values()) el.remove();
  children.forEach((el, i) => {
    if (old.children[i] !== el) old.insertBefore(el, old.children[i] || null);
  });
  SOURCE.set(old, source);
  return old;
}

// --- custom elements (controller) ------------------------------------------------------------

function defineElements() {
  class SteamEngineCard extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this._config = null;
      this._renderer = RENDERERS.tiles;
      this._hass = null;
      this._ids = null;
      this._idsError = null;
      this._fetching = false;
      this._lastFetch = 0;
      this._seen = new Map();
      this._locale = ''; // formatLocaleOf, see _statesChanged
      this._localeKey = '';
      this._busy = new Set();
      this._pcPhase = ''; // the last PC state seen, for phaseFailure
      this._dayTimer = null; // renders again after midnight while the hint names a day
      this._messages = [];
      this._messageId = 0;
      this._timers = new Map();
      this._tracked = new Map();
      this._sending = new Set();
      this._confirm = null;
      this._sheet = null; // { appid, name, list } of the capsule tapped while its sheet is open
      this._openDrive = ''; // letter of the drive whose games the tab Space lists ('' for none)
      this._lastSheet = '';
      this._sheetFocusKey = '';
      // Install sizes asked from steam_engine/game_size, app ID -> { loading: true } or gameSizeEntry
      this._gameSizes = new Map();
      this._failedImages = new Set(); // image URLs that did not load
      this._imageRender = null;
      // Image URLs the integration resolved (steam_engine/subscribe_images), see _connectImages
      this._images = {};
      this._imagesSig = '';
      this._imagesConn = null; // the connection the subscription is on
      this._imagesSub = null; // { unsub } of the subscription (unsub null while it is being made)
      this._imagesAttempt = 0; // failed tries since the last subscription
      this._imagesRetry = null; // timer of the next try
      this._entitiesWereMissing = false; // _entityMissing() at the last hass, see imagesResubscribeDue
      this._knownMissing = []; // missing right after the last steam_engine/entities, see splitMissing
      // The connection is back after a break: the subscription ended with the old socket
      this._onReady = () => this._resubscribeImages();
      this._tab = stored(TAB_STORAGE_KEY);
      this._details = detailsOpen(stored(DETAILS_STORAGE_KEY));
      this._built = false;
      this._lastBody = '';
      this._parts = [];
      this._focusKey = '';
      this._model = null;
      this._onNavigate = () => {
        this._dismissDialog();
        this._closeSheet();
      };
    }

    static getConfigElement() {
      return document.createElement(EDITOR_TYPE);
    }

    static getStubConfig() {
      return {};
    }

    setConfig(config) {
      const before = this._config;
      this._config = normalizeConfig(config);
      if (before && before.pc !== this._config.pc) this._changePc();
      const renderer = rendererFor(this._config);
      if (renderer !== this._renderer) {
        this._dismissDialog();
        this._closeSheet();
        this._renderer = renderer;
        this._built = false; // a different style builds the card again
        this._parts = [];
      }
      this._lastBody = '';
      this._render();
    }

    set hass(hass) {
      this._hass = hass;
      if (!this._ids) {
        this._fetchIds();
        if (!this._built) this._render();
        return;
      }
      this._connectImages();
      const missing = this._entityMissing();
      if (missing) this._fetchIds();
      else if (imagesResubscribeDue(this._entitiesWereMissing, missing, !!this._imagesSub)) this._resubscribeImages();
      this._entitiesWereMissing = missing;
      if (this._statesChanged()) {
        this._checkTracked();
        this._render();
      }
    }

    get hass() {
      return this._hass;
    }

    // Messages and watched jobs survive a detach (a pop-up that closes); their timers only touch
    // this element's own list. An open confirm dialog does not: it is closed when the card leaves
    // the page or the user navigates (a pop-up closed with the back gesture may only be hidden), and
    // neither does a game's sheet.
    connectedCallback() {
      window.addEventListener('location-changed', this._onNavigate);
      window.addEventListener('popstate', this._onNavigate);
      this._connectImages();
      if (this._config && this._hass) this._render();
    }

    disconnectedCallback() {
      window.removeEventListener('location-changed', this._onNavigate);
      window.removeEventListener('popstate', this._onNavigate);
      this._dismissDialog();
      this._closeSheet();
      this._scheduleDayChange(null);
      this._disconnectImages();
    }

    // --- resolved image URLs -------------------------------------------------------------
    // The subscription steam_engine/subscribe_images sends the whole map at once and again whenever
    // the integration resolved more games (also to users who are not administrators). The card
    // subscribes once the entity IDs are known, on its own connection: again when hass brings a new
    // one, when the card is attached again and when the connection is back after a break (the
    // subscription is made with resubscribe off: the card resubscribes itself, so it sees a
    // refusal). A refused subscription (the integration not loaded yet while Home Assistant starts)
    // is tried again after IMAGES_RETRY_MS, and at once when the
    // integration's entities appear in hass.states (it was loaded) or the connection is back after
    // a break. Until then the card keeps the CDN's old paths.

    _connectImages() {
      const conn = this._hass && this._hass.connection;
      if (!conn || !this._ids || !this.isConnected || conn === this._imagesConn) return;
      this._disconnectImages();
      this._imagesConn = conn;
      if (typeof conn.addEventListener === 'function') conn.addEventListener('ready', this._onReady);
      this._subscribeImages();
    }

    _subscribeImages() {
      const conn = this._imagesConn;
      if (!conn || this._imagesSub || this._imagesRetry || typeof conn.subscribeMessage !== 'function') return;
      const sub = { unsub: null };
      this._imagesSub = sub;
      Promise.resolve()
        .then(() =>
          conn.subscribeMessage(
            (event) => {
              if (this._imagesSub === sub) this._applyImages(normalizeImages(event));
            },
            { type: `${DOMAIN}/subscribe_images`, ...pcField(this._config) },
            { resubscribe: false },
          ),
        )
        .then((unsub) => {
          if (this._imagesSub !== sub) {
            dropSubscription(unsub); // disconnected or resubscribed in the meantime
            return;
          }
          sub.unsub = unsub;
          this._imagesAttempt = 0;
        })
        .catch(() => {
          if (this._imagesSub !== sub) return;
          this._imagesSub = null;
          const delay = imagesRetryDelay(this._imagesAttempt);
          this._imagesAttempt += 1;
          if (!delay) return;
          this._imagesRetry = setTimeout(() => {
            this._imagesRetry = null;
            this._subscribeImages();
          }, delay);
        });
    }

    /** Subscribes again now (the connection is back, or the integration's entities appeared). A subscription from before a reconnect ended with the old socket; its unsub is not
     * called, its message ID may belong to another command on the new one. */
    _resubscribeImages() {
      if (!this._imagesConn) return;
      this._imagesSub = null;
      this._imagesAttempt = 0;
      if (this._imagesRetry) clearTimeout(this._imagesRetry);
      this._imagesRetry = null;
      this._subscribeImages();
    }

    _disconnectImages() {
      const conn = this._imagesConn;
      if (conn && typeof conn.removeEventListener === 'function') conn.removeEventListener('ready', this._onReady);
      if (this._imagesSub) dropSubscription(this._imagesSub.unsub);
      this._imagesSub = null;
      this._imagesConn = null;
      this._imagesAttempt = 0;
      if (this._imagesRetry) clearTimeout(this._imagesRetry);
      this._imagesRetry = null;
    }

    /** Takes the resolved images; the body (and an open sheet) render once, only when they changed.
     * The parts morph in place, so only the images whose URL changed load. */
    _applyImages(images) {
      const sig = imagesSignature(images);
      if (sig === this._imagesSig) return;
      this._imagesSig = sig;
      this._images = images;
      if (this._built && this._ids && this._hass) this._renderBody();
    }

    /** While automatic work waits for the active hours, the card renders again just after midnight
     * (in the time zone of Home Assistant, as the hint):
     * the hint names the day ("tomorrow" becomes "today"), and the states may not change then. */
    _scheduleDayChange(m) {
      if (!m || !m.deferredUntil || !this.isConnected) {
        if (this._dayTimer) clearTimeout(this._dayTimer);
        this._dayTimer = null;
        return;
      }
      if (this._dayTimer) return;
      this._dayTimer = setTimeout(() => {
        this._dayTimer = null;
        this._lastBody = '';
        this._lastSheet = '';
        this._renderBody();
      }, msToNextDay(new Date(), m.timeZone));
    }

    getCardSize() {
      return this._config ? this._renderer.cardSize(this._config) : 3;
    }

    getGridOptions() {
      return { columns: 12, min_columns: 6 };
    }

    // --- entity IDs and states -----------------------------------------------------------

    /** The card shows another PC now: its entity IDs and images are fetched anew. */
    _changePc() {
      this._disconnectImages();
      this._ids = null;
      this._idsError = null;
      this._lastFetch = 0;
      this._knownMissing = [];
      this._images = {};
      this._imagesSig = '';
      this._seen.clear();
      this._model = null;
      this._lastBody = '';
      this._fetchIds();
    }

    _fetchIds() {
      if (!this._hass || this._fetching || !this._config) return;
      if (this._lastFetch && Date.now() - this._lastFetch < REFETCH_MS) return;
      this._fetching = true;
      this._lastFetch = Date.now();
      const pc = this._config.pc;
      this._hass
        .callWS({ type: `${DOMAIN}/entities`, ...pcField(this._config) })
        .then((result) => {
          if (!this._config || this._config.pc !== pc) return; // the card was set to another PC meanwhile
          this._ids = result && typeof result === 'object' ? result : { entities: {}, agent: {} };
          this._idsError = null;
          // What is missing now is not asked for again while it stays so (splitMissing)
          this._knownMissing = missingEntities(this._ids.entities, this._hass && this._hass.states);
          this._seen.clear();
          this._statesChanged();
          this._render();
          this._connectImages();
        })
        .catch((err) => {
          if (!this._config || this._config.pc !== pc) return;
          if (!this._ids) {
            this._idsError = err || {};
            this._render();
          }
        })
        .finally(() => {
          this._fetching = false;
        });
    }

    _ownIds() {
      return watchedIds(this._ids);
    }

    /** An entity of the integration is not in hass.states, and was not missing right after the
     * last steam_engine/entities: it may have been renamed. One the answer named while it was
     * missing already (a disabled entity, or one left out) does not count (splitMissing). */
    _entityMissing() {
      const own = (this._ids && this._ids.entities) || {};
      const split = splitMissing(missingEntities(own, this._hass && this._hass.states), this._knownMissing);
      this._knownMissing = split.known;
      return split.fresh.length > 0;
    }

    /** Remembers the state objects of the card's entities; true when one of them (or the
     * language or the formats of the profile) changed. Home Assistant replaces a state object on
     * every change. */
    _statesChanged() {
      const states = (this._hass && this._hass.states) || {};
      let changed = false;
      const locale = formatLocaleOf(this._hass);
      const localeKey = JSON.stringify(locale);
      if (localeKey !== this._localeKey) {
        this._localeKey = localeKey;
        this._locale = locale;
        changed = true;
      }
      const ids = this._ownIds();
      for (const id of ids) {
        if (this._seen.get(id) !== states[id]) {
          this._seen.set(id, states[id]);
          changed = true;
        }
      }
      if (this._seen.size !== ids.length) {
        for (const id of [...this._seen.keys()]) if (!ids.includes(id)) this._seen.delete(id);
      }
      return changed;
    }

    _state(group, key) {
      const id = this._ids && this._ids[group] && this._ids[group][key];
      return id && this._hass ? this._hass.states[id] : undefined;
    }

    get _lang() {
      return languageOf(this._hass);
    }

    /** What the renderer gets: the model and the card's own state, nothing to call. */
    _view() {
      const m = this._model;
      return {
        lang: this._lang,
        locale: this._locale,
        config: this._config,
        m,
        tab: m ? selectedTab(cardTabs(this._config, m), this._tab) : '',
        details: this._details,
        openDrive: this._openDrive,
        busy: (key) => this._busy.has(key),
        failed: (url) => this._failedImages.has(url),
        gameSize: (appid) => this._gameSizes.get(String(appid)),
      };
    }

    // --- rendering -----------------------------------------------------------------------

    _build() {
      const root = this.shadowRoot;
      root.innerHTML = `<style>${this._renderer.styles}</style>${this._renderer.shell()}`;
      if (!this._listening) {
        this._listening = true;
        root.addEventListener('click', (ev) => this._onClick(ev));
        root.addEventListener('keydown', (ev) => this._onKeydown(ev));
        // A drive picked in the install dialog: kept with the request, the warnings follow it
        root.addEventListener('change', (ev) => {
          const target = ev.target;
          const request = this._confirm;
          if (!target || target.name !== 'drive' || !request || !['install', 'move'].includes(request.action)) return;
          request.drive = target.value;
          this._refreshDialog();
        });
        // Images that do not load (no picture on the CDN) are hidden, with their frame when they
        // have one (data-img), and remembered: the renderer leaves them out from then on (the
        // library style shows its placeholder instead). error does not bubble.
        root.addEventListener('error', (ev) => {
          const target = ev.target;
          if (!target || target.tagName !== 'IMG') return;
          const frame = target.closest('[data-img]');
          (frame || target).hidden = true;
          const url = target.getAttribute('src');
          if (!url || this._failedImages.has(url)) return;
          this._failedImages.add(url);
          // One render for a burst of failed images
          if (!this._imageRender) {
            this._imageRender = setTimeout(() => {
              this._imageRender = null;
              this._lastBody = '';
              this._lastSheet = '';
              this._renderBody();
            }, 50);
          }
        }, true);
      }
      const dialog = this._confirmDialog();
      dialog.addEventListener('close', () => {
        // The event comes later: a dialog that was opened again since keeps its request
        if (!dialog.open) this._confirm = null;
      });
      const sheet = root.querySelector('dialog.sheet');
      if (sheet) {
        // Closed by the browser (Escape): the event comes later, a sheet opened again meanwhile stays
        sheet.addEventListener('close', () => {
          if (!sheet.open) this._sheetClosed();
        });
        // A tap on the backdrop hits the dialog itself, not its content
        sheet.addEventListener('click', (ev) => {
          if (ev.target === sheet) this._closeSheet();
        });
      }
      this._confirm = null;
      this._sheet = null;
      this._lastSheet = '';
      this._parts = [];
      this._lastBody = '';
      this._built = true;
      this._renderMessages();
    }

    _render() {
      if (!this._config || !this._hass) return;
      if (!this._built) this._build();
      const root = this.shadowRoot;
      const title = root.querySelector('.title');
      title.hidden = !this._config.title;
      title.textContent = this._config.title;
      this._renderBody();
      this._renderMessages();
    }

    // The body is one element per part; only parts whose HTML changed are replaced, so a progress
    // tick in the status area does not rebuild (and swallow taps on) the list buttons.
    _renderBody() {
      if (!this._built) return;
      const lang = this._lang;
      let parts; // [key, html of one element]
      if (!this._ids) {
        const text = !this._idsError ? t(lang, 'loading') : loadErrorText(lang, this._idsError, this._config);
        parts = [['hint', `<p class="hint">${escapeHtml(text)}</p>`]];
      } else {
        this._model = buildModel(this._hass.states, this._ids, this._hass.config && this._hass.config.time_zone, this._images);
        this._notePcPhase(this._model.pcPhase);
        this._scheduleDayChange(this._model);
        this._refreshPcDialog();
        parts = this._renderer.parts(this._view()).filter((p) => p[1]);
      }
      const html = parts.map((p) => p[1]).join('');
      this._renderSheet();
      if (html === this._lastBody) return;
      this._lastBody = html;
      const body = this.shadowRoot.querySelector('.body');
      const active = this.shadowRoot.activeElement;
      let key = active && active.dataset ? active.dataset.key : '';
      // Focus lost to a control that was busy (disabled) comes back once it is enabled again,
      // unless the user has focused something else in the meantime
      const lost = !document.activeElement || document.activeElement === document.body;
      if (!key && this._focusKey && lost) key = this._focusKey;
      this._focusKey = '';
      // Parts are matched by key: an unchanged one stays, a changed one is updated (in place where
      // it allows it, see morphElement), so switching a tab does not rebuild the parts above
      const previous = this._parts;
      const nodes = [...body.children];
      const old = new Map();
      if (nodes.length === previous.length) previous.forEach((p, i) => old.set(p[0], { html: p[1], node: nodes[i] }));
      const placed = parts.map(([partKey, partHtml]) => {
        const before = old.get(partKey);
        if (!before) return htmlElement(partHtml);
        old.delete(partKey);
        return before.html === partHtml ? before.node : morphElement(before.node, htmlElement(partHtml));
      });
      for (const el of [...body.children]) if (!placed.includes(el)) el.remove();
      placed.forEach((el, i) => {
        if (body.children[i] !== el) body.insertBefore(el, body.children[i] || null);
      });
      this._parts = parts;
      // A tab bar that scrolls (library style) shows the selected tab, but only when the tab
      // changed or the bar is new: a progress tick must not undo the user's own scrolling. A bar
      // that is not laid out yet (card hidden) is revealed on a later render.
      const tablist = body.querySelector('[role="tablist"]');
      const current = tablist ? tablist.querySelector('[aria-selected="true"]') : null;
      const currentKey = current ? current.id || current.dataset.key || '' : '';
      let selected = null;
      if (current && tablist.clientWidth > 0
        && (tablist !== this._revealedBar || currentKey !== this._revealedTab)) {
        this._revealedBar = tablist;
        this._revealedTab = currentKey;
        if (tablist.scrollWidth > tablist.clientWidth) selected = current;
      }
      if (selected) {
        const box = tablist.getBoundingClientRect();
        const tab = selected.getBoundingClientRect();
        if (tab.right > box.right) tablist.scrollLeft += tab.right - box.right + 8;
        else if (tab.left < box.left) tablist.scrollLeft -= box.left - tab.left + 8;
      }
      if (key) {
        const again = body.querySelector(`[data-key="${key}"]`);
        if (again && !again.disabled) again.focus();
        else if (again) this._focusKey = key;
      }
    }

    // The message box is a live region: messages are added and removed one by one, so a screen
    // reader announces only the new one instead of reading all remaining messages again.
    _renderMessages() {
      if (!this._built) return;
      const box = this.shadowRoot.querySelector('.messages');
      const ids = new Set(this._messages.map((msg) => String(msg.id)));
      for (const el of [...box.children]) if (!ids.has(el.dataset.id)) el.remove();
      const shown = new Set([...box.children].map((el) => el.dataset.id));
      const view = this._view();
      for (const msg of this._messages) {
        if (shown.has(String(msg.id))) continue;
        box.insertAdjacentHTML('beforeend', this._renderer.message(view, msg));
      }
    }

    // --- the sheet of a game (library style) ---------------------------------------------

    _sheetDialog() {
      return this._built ? this.shadowRoot.querySelector('dialog.sheet') : null;
    }

    _confirmDialog() {
      return this.shadowRoot.querySelector('dialog:not(.sheet)');
    }

    /** Opens the sheet of the capsule tapped; it keeps that app ID whatever the lists do next. */
    _openSheet(request) {
      const sheet = this._sheetDialog();
      if (!sheet || !this._renderer.sheet || !request.appid) return;
      this._sheet = request;
      this._lastSheet = '';
      this._renderSheet();
      if (!this._sheet) return; // the game is gone already
      if (typeof sheet.showModal === 'function') {
        if (sheet.open) sheet.close();
        this._sheet = request; // close() above may have cleared it
        sheet.showModal();
      } else {
        sheet.classList.add('fallback');
        sheet.setAttribute('open', '');
      }
      // The close button first: Enter right after the tap must not start or uninstall anything
      const close = sheet.querySelector('[data-action="sheet_close"]');
      if (close) close.focus();
    }

    /** Renders the open sheet from the current model; it closes once the game is in no list. */
    _renderSheet() {
      const sheet = this._sheetDialog();
      if (!sheet || !this._sheet || !this._model || !this._renderer.sheet) return;
      const detail = gameDetail(this._lang, this._sheet.appid, this._model);
      if (!detail.known) {
        this._closeSheet();
        return;
      }
      if (wantsGameSize(detail)) this._askGameSize(detail.appid);
      const html = this._renderer.sheet(this._view(), detail, sheetActions(detail, this._model, this._config));
      if (html === this._lastSheet) return;
      this._lastSheet = html;
      const active = this.shadowRoot.activeElement;
      let key = active && sheet.contains(active) && active.dataset ? active.dataset.key : '';
      if (!key && this._sheetFocusKey) key = this._sheetFocusKey;
      this._sheetFocusKey = '';
      const next = htmlElement(html);
      if (sheet.firstElementChild) morphElement(sheet.firstElementChild, next);
      else sheet.appendChild(next);
      // A control that was busy (disabled) gets the focus back once it is enabled again
      if (key) {
        const again = sheet.querySelector(`[data-key="${key}"]`);
        if (again && !again.disabled) {
          if (this.shadowRoot.activeElement !== again) again.focus();
        } else if (again) {
          this._sheetFocusKey = key;
        }
      }
    }

    _closeSheet() {
      const sheet = this._sheetDialog();
      if (sheet && sheet.hasAttribute('open')) {
        if (typeof sheet.close === 'function' && sheet.open) {
          sheet.close();
        } else {
          sheet.removeAttribute('open');
          sheet.dispatchEvent(new Event('close'));
        }
      }
      this._sheetClosed();
    }

    /** Forgets the closed sheet and gives the focus back to the capsule that opened it. */
    _sheetClosed() {
      if (!this._sheet) return;
      const key = `sheet:${this._sheet.list}:${this._sheet.appid}`;
      this._sheet = null;
      this._sheetFocusKey = '';
      const tile = this._built && this.isConnected ? this.shadowRoot.querySelector(`[data-key="${key}"]`) : null;
      if (tile) tile.focus({ preventScroll: true });
    }

    /** An action of the sheet is still offered by the current states (the sheet may have stayed
     * open while the game started, left the library or went into the job). */
    _sheetAllows(action, appid) {
      if (!this._hass || !this._ids) return false;
      const m = buildModel(this._hass.states, this._ids);
      return sheetActions(gameDetail(this._lang, appid, m), m, this._config).includes(action);
    }

    // --- messages and watched jobs -------------------------------------------------------

    _addMessage(type, text, ms = MESSAGE_MS[type]) {
      const id = ++this._messageId;
      this._messages.push({ id, type, text });
      while (this._messages.length > MAX_MESSAGES) this._dropMessage(this._messages[0].id, false);
      if (ms) this._timers.set(id, setTimeout(() => this._dropMessage(id), ms));
      if (this._built) this._renderMessages();
      return id;
    }

    _dropMessage(id, render = true) {
      const timer = this._timers.get(id);
      if (timer) clearTimeout(timer);
      this._timers.delete(id);
      this._messages = this._messages.filter((m) => m.id !== id);
      if (render && this._built) this._renderMessages();
    }

    _checkTracked() {
      if (!this._tracked.size && !this._sending.size) return;
      const lang = this._lang;
      const library = this._state('agent', 'library');
      const status = this._state('agent', 'install_status');
      const now = Date.now();
      // The job may start before the action's answer arrives: note it for the games still being sent
      for (const sending of this._sending) {
        for (const appid of sending.appids) if (jobRunsFor(appid, status)) sending.ran.add(appid);
      }
      const progress = this._state('entities', 'download_progress');
      const watch = { uninstall: UNINSTALL_WATCH_MS, move: MOVE_WATCH_MS };
      for (const [key, tracked] of [...this._tracked]) {
        if (!watch[tracked.action] && jobRunsFor(tracked.appid, status)) tracked.ran = true;
        if (tracked.action === 'move') {
          const running = moveInfo(progress);
          if (running && running.appid === String(tracked.appid)) tracked.seen = true;
        }
        const message = completionMessage(lang, tracked, library, status, progress);
        if (message) {
          this._tracked.delete(key);
          this._addMessage(message.type, message.text);
        } else if (now - tracked.since > (watch[tracked.action] || JOB_WATCH_MS)) {
          this._tracked.delete(key);
        }
      }
    }

    // --- actions -------------------------------------------------------------------------

    /** Shows a tab and remembers it in this browser; focus moves along when asked (arrow keys). */
    _selectTab(tab, focus) {
      this._tab = tab;
      store(TAB_STORAGE_KEY, tab);
      this._renderBody();
      if (focus) {
        const button = this.shadowRoot.querySelector(`[data-key="tab:${tab}"]`);
        if (button) button.focus();
      }
    }

    _onKeydown(ev) {
      const target = ev.target && ev.target.closest ? ev.target.closest('[data-action="tab"]') : null;
      if (!target || !this._model) return;
      const next = nextTab(cardTabs(this._config, this._model), target.dataset.tab, ev.key);
      if (!next) return;
      ev.preventDefault();
      this._selectTab(next, true);
    }

    _onClick(ev) {
      const target = ev.target && ev.target.closest ? ev.target.closest('[data-action], [data-dialog]') : null;
      if (!target || target.disabled) return;
      if (target.dataset.dialog) {
        this._closeDialog(target.dataset.dialog === 'ok');
        return;
      }
      const { action, appid, name } = target.dataset;
      const m = this._model;
      // A game action in the sheet: checked against the current states. Start and update close
      // the sheet (their answer shows in the card's messages); the switch keeps it open, install
      // and uninstall ask first (see _closeDialog)
      const sheet = this._sheetDialog();
      const fromSheet = !!sheet && sheet.contains(target);
      if (fromSheet && ['launch', 'update', 'uninstall', 'install', 'move', 'auto', 'mute'].includes(action)) {
        if (!this._sheetAllows(action === 'mute' ? 'auto' : action, appid)) {
          this._lastSheet = '';
          this._renderSheet();
          return;
        }
        if (action === 'launch' || action === 'update') this._closeSheet();
      }
      switch (action) {
        case 'sheet':
          this._openSheet({ appid, name, list: target.dataset.list || '' });
          break;
        case 'sheet_close':
          this._closeSheet();
          break;
        case 'tab':
          this._selectTab(target.dataset.tab, false);
          break;
        case 'drive':
          this._openDrive = this._openDrive === target.dataset.drive ? '' : target.dataset.drive || '';
          this._renderBody();
          break;
        case 'details':
          this._details = !this._details;
          store(DETAILS_STORAGE_KEY, this._details ? '1' : '0');
          this._renderBody();
          break;
        case 'dismiss':
          this._dropMessage(Number(target.dataset.id));
          break;
        case 'uninstall':
        case 'install':
        case 'move':
          this._openDialog({ action, appid, name, size: Number(target.dataset.size) || 0, fromSheet });
          break;
        case 'update':
          this._run(`update:${appid}`, 'update', 'install', { appid }, [{ appid, name }]);
          break;
        case 'launch':
          this._run(`launch:${appid}`, 'launch', 'launch', { appid }, [{ appid, name }]);
          break;
        case 'auto': {
          const enabled = target.getAttribute('aria-checked') !== 'true';
          this._run(`auto:${appid}`, 'auto_update', 'set_auto_update', { appid, enabled }, [{ appid, name }]);
          break;
        }
        case 'mute': {
          const enabled = target.getAttribute('aria-checked') !== 'true';
          this._run(`mute:${appid}`, 'muted', 'set_muted', { appid, enabled }, [{ appid, name }]);
          break;
        }
        case 'install_all': {
          if (!m) return;
          // Exactly the games shown without a job or queue mark at the time of the tap
          const games = countedUpdates(m).filter((g) => !m.open.has(g.appid))
            .map((g) => ({ appid: g.appid, name: g.name || gameName(this._lang, m.names, g.appid) }));
          if (!games.length) return;
          this._run('install_all', 'update', 'install', { appid: games.map((g) => g.appid) }, games);
          break;
        }
        case 'check':
          this._run('check', 'check', 'refresh', {}, []);
          break;
        case 'purchases':
          if (m) this._switchEntity('purchases:all', m.purchasesEntity, target.getAttribute('aria-checked') !== 'true', 'auto_purchases');
          break;
        case 'auto_off':
          if (m) this._switchEntity('auto_off:all', m.autoOffEntity, target.getAttribute('aria-checked') !== 'true', 'auto_off');
          break;
        case 'pc': {
          // Shut down, restart, the forced shut down and close games ask first; wake and lock do not
          const pc = target.dataset.pc;
          if (PC_CONFIRM.includes(pc)) this._openDialog({ action: `pc_${pc}` });
          else this._runPc(pc);
          break;
        }
        default:
          break;
      }
    }

    /** The install size of a game that is not installed, asked once per card (the integration
     * keeps it for days); the open sheet and install dialog of the game show it when it comes. An
     * error counts as unknown and is asked again when the game is opened after GAME_SIZE_RETRY_MS. */
    _askGameSize(appid) {
      const id = String(appid || '');
      if (!id || !needsGameSize(this._gameSizes.get(id), Date.now())) return;
      if (!this._hass || typeof this._hass.callWS !== 'function') return;
      this._gameSizes.set(id, { loading: true });
      this._hass
        .callWS({ type: `${DOMAIN}/game_size`, appid: id, ...pcField(this._config) })
        .then((result) => gameSizeEntry(result), () => gameSizeEntry(null))
        .then((entry) => {
          this._gameSizes.set(id, { ...entry, at: Date.now() });
          if (this._sheet && this._sheet.appid === id) this._renderSheet();
          const request = this._confirm;
          if (this._built && request && request.action === 'install' && request.appid === id) this._refreshDialog();
        });
    }

    /** Brings the open install dialog up to date (the install size came, or another drive was
     * picked): its text, warnings, hint and each drive's free space and fit, without a new render that would take
     * the focus away. */
    _refreshDialog() {
      const request = this._confirm;
      if (!this._built || !request) return;
      const dialog = this._confirmDialog();
      const c = this._confirmContent(request);
      const text = dialog.querySelector('#dialog-text');
      if (text) text.textContent = c.text;
      const warnings = dialog.querySelector('[data-warnings]');
      if (warnings) warnings.innerHTML = dialogWarnings(c);
      const hint = dialog.querySelector('[data-hint]');
      if (hint) {
        hint.textContent = c.driveHint || '';
        hint.hidden = !c.driveHint;
      }
      dialog.querySelectorAll('input[name="drive"]').forEach((input) => {
        const d = c.drives.find((x) => x.letter === input.value);
        const row = input.closest('.dopt');
        if (!d || !row) return;
        input.checked = d.checked;
        const free = row.querySelector('.df');
        if (free) {
          free.className = driveFreeClass(d);
          free.textContent = driveFreeText(d);
        }
      });
    }

    /** An open confirmation of a PC action follows the states: a game, a job or a download that
     * starts while it is open shows among its warnings before the user confirms. Only the
     * warnings change, the focus stays. */
    _refreshPcDialog() {
      const request = this._confirm;
      if (!this._built || !request || !pcDialogAction(request.action)) return;
      const html = dialogWarnings(this._confirmContent(request));
      if (html === request.warnings) return;
      request.warnings = html;
      const warnings = this._confirmDialog().querySelector('[data-warnings]');
      if (warnings) warnings.innerHTML = html;
    }

    /** confirmContent with the install size known for a game to install. */
    _confirmContent(request) {
      const size = request.action === 'install' ? knownGameSize(this._gameSizes.get(String(request.appid))) : request.size;
      return confirmContent(this._lang, { ...request, size }, this._model, this._config, this._locale);
    }

    _openDialog(request) {
      const dialog = this._confirmDialog();
      this._confirm = request; // the app ID and name of the row tapped, whatever the list does next
      if (request.action === 'install') this._askGameSize(request.appid);
      const content = this._confirmContent(request);
      request.warnings = dialogWarnings(content);
      dialog.innerHTML = this._renderer.dialog(this._view(), content);
      if (typeof dialog.showModal === 'function') {
        // A dialog left open without being modal (its card was detached meanwhile) is reopened modal
        if (dialog.open) dialog.close();
        dialog.showModal();
      } else {
        dialog.classList.add('fallback');
        dialog.setAttribute('open', '');
      }
      const cancel = dialog.querySelector('[data-dialog="cancel"]');
      if (cancel) cancel.focus();
    }

    /** Closes an open confirm dialog without running its action. */
    _dismissDialog() {
      if (!this._built) return;
      if (this._confirmDialog().hasAttribute('open')) this._closeDialog(false);
    }

    _closeDialog(confirmed) {
      const request = this._confirm;
      const dialog = this._confirmDialog();
      // The drive picked in an install dialog that offered a choice ('' without one)
      const picked = dialog.querySelector('input[name="drive"]:checked');
      const drive = picked ? picked.value : '';
      if (typeof dialog.close === 'function' && dialog.open) dialog.close();
      else dialog.removeAttribute('open');
      this._confirm = null;
      if (!confirmed || !request) return;
      if (request.fromSheet) {
        // Install or uninstall from a sheet: still offered now? Then the sheet makes way for the
        // card's messages
        if (!this._sheetAllows(request.action, request.appid)) {
          this._lastSheet = '';
          this._renderSheet();
          return;
        }
        this._closeSheet();
      }
      const pc = pcDialogAction(request.action);
      if (pc) {
        this._runPc(pc);
        return;
      }
      const games = [{ appid: request.appid, name: request.name }];
      if (request.action === 'uninstall') {
        this._run(`uninstall:${request.appid}`, 'uninstall', 'uninstall', { appid: request.appid }, games);
      } else if (request.action === 'move') {
        if (drive) this._run(`move:${request.appid}`, 'move', 'move', { appid: request.appid, drive }, games);
      } else {
        // With a choice the drive is always sent, also Steam's default: that one is then fixed even
        // if the star moves before the job runs, and it replaces a drive picked before
        this._run(`install:${request.appid}`, 'install', 'install', installData(request.appid, drive), games);
      }
    }

    async _run(key, action, service, data, games) {
      if (this._busy.has(key) || !this._hass) return;
      const lang = this._lang;
      const name = games.map((g) => g.name).join(', ');
      this._busy.add(key);
      this._lastBody = '';
      this._renderBody();
      let waking = 0;
      if (this._model && !this._model.pcOn && ['install', 'update', 'uninstall', 'launch', 'move'].includes(action)) {
        waking = this._addMessage('info', t(lang, 'msg_waking', { name }), 0);
      }
      // A move's end is told by the download progress sensor, a job's by the job state
      const mark = action === 'move'
        ? moveMark(moveResultOf(this._state('entities', 'download_progress')))
        : statusSignature(this._state('agent', 'install_status'));
      const sending = { appids: games.map((g) => g.appid), ran: new Set() };
      if (action === 'install' || action === 'update') this._sending.add(sending);
      try {
        const result = await this._hass.callWS({
          type: 'call_service',
          domain: DOMAIN,
          service,
          service_data: { ...data, ...pcField(this._config) },
          return_response: true,
        });
        const message = responseMessage(lang, action, name, result ? result.response : undefined);
        if (message) {
          this._addMessage(message.type, message.text);
          if (message.track) {
            for (const game of games) {
              this._tracked.set(`${action}:${game.appid}`, {
                action, appid: game.appid, name: game.name, mark, ran: sending.ran.has(game.appid), since: Date.now(),
              });
            }
          }
        }
      } catch (err) {
        this._addMessage('error', t(lang, 'msg_error', { name: name || t(lang, 'card_name'), error: errorText(err) }));
      } finally {
        this._sending.delete(sending);
        if (waking) this._dropMessage(waking);
        this._busy.delete(key);
        this._lastBody = '';
        this._renderBody();
        this._checkTracked();
      }
    }

    /** A PC action: wake, shutdown, restart, lock, close_games or force_shutdown. The switch and
     * button services answer nothing, so the call is made without return_response; a refused shut
     * down (the PC switch's, not the forced one) comes back as an error that
     * names the reason. The button must still be shown by the current states (a confirm dialog may
     * have stayed open while the agent went offline), and an action the PC state shows on its way is not sent again. */
    async _runPc(action) {
      const key = `pc:${action}`;
      if (this._busy.has(key) || !this._hass || !this._ids) return;
      const m = buildModel(this._hass.states, this._ids);
      if (!pcActions(m)[action]) return;
      if (pcActionPending(m, action)) return;
      const call = pcServiceCall(action, m);
      if (!call) return;
      const lang = this._lang;
      this._busy.add(key);
      this._lastBody = '';
      this._renderBody();
      try {
        await this._hass.callWS({ type: 'call_service', ...call });
        this._addMessage(action === 'wake' ? 'info' : 'success', t(lang, `msg_pc_${action}`));
      } catch (err) {
        this._addMessage('error', t(lang, 'msg_error', { name: t(lang, `pc_${action}`), error: errorText(err) }));
      } finally {
        this._busy.delete(key);
        this._lastBody = '';
        this._renderBody();
      }
    }

    /** A message when the PC state left an action without its result (phaseFailure); only for a
     * change this card saw (not when it opens or the sensor was unknown). */
    _notePcPhase(phase) {
      const failed = phaseFailure(this._pcPhase, phase);
      this._pcPhase = phase;
      if (failed) this._addMessage('error', t(this._lang, failed));
    }

    /** Switches one of the integration's own switches (new purchases, auto off); the switch shows
     * the new state once Home Assistant reports it. */
    async _switchEntity(key, entityId, on, labelKey) {
      if (!entityId || !this._hass || this._busy.has(key)) return;
      this._busy.add(key);
      this._lastBody = '';
      this._renderBody();
      try {
        await this._hass.callWS({
          type: 'call_service',
          domain: 'switch',
          service: on ? 'turn_on' : 'turn_off',
          target: { entity_id: entityId },
        });
      } catch (err) {
        this._addMessage('error', t(this._lang, 'msg_error', { name: t(this._lang, labelKey), error: errorText(err) }));
      } finally {
        this._busy.delete(key);
        this._lastBody = '';
        this._renderBody();
      }
    }
  }

  // The editor: title, style, game images, actions while the PC is off and, with several PCs, the
  // card's PC. sections stays a YAML option. Uses ha-form when
  // the frontend has it, plain inputs otherwise.
  const EDITOR_FIELDS = ['title', 'style', 'show_images', 'actions_when_pc_off', 'pc'];

  class SteamEngineCardEditor extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this._config = null;
      this._hass = null;
      this._form = null;
      this._waiting = false;
      this._pcs = null; // names of the PCs (steam_engine/pcs), "" for the PC without a name
      this._pcsAsked = false;
    }

    /** Asks the integration once for its PCs; the PC field shows only with more than one. */
    _askPcs() {
      if (this._pcsAsked || !this._hass || typeof this._hass.callWS !== 'function') return;
      this._pcsAsked = true;
      this._hass
        .callWS({ type: `${DOMAIN}/pcs` })
        .then((result) => {
          this._pcs = editorPcs(result);
          this._form = null; // the schema changes
          this._render();
        })
        .catch(() => {});
    }

    setConfig(config) {
      this._config = { ...(config || {}) };
      this._render();
    }

    set hass(hass) {
      this._hass = hass;
      this._askPcs();
      if (this._form) this._form.hass = hass;
      else this._render();
    }

    /** The PCs to choose from, or [] when there is only one (and the card has none set). */
    _pcChoices() {
      const pcs = this._pcs || [];
      const own = this._config && this._config.pc ? String(this._config.pc).trim() : '';
      if (pcs.length < 2 && !own) return [];
      const choices = pcs.map((pc) => pc || UNNAMED_PC);
      if (own && !choices.some((c) => c.toLowerCase() === own.toLowerCase())) choices.push(own);
      return choices;
    }

    _data() {
      const c = this._config || {};
      const pcs = this._pcs || [];
      return {
        title: c.title === undefined || c.title === null ? '' : String(c.title),
        style: normalizeStyle(c.style),
        show_images: c.show_images !== false,
        actions_when_pc_off: c.actions_when_pc_off !== false,
        pc: c.pc ? String(c.pc).trim() : (pcs.includes('') ? UNNAMED_PC : ''),
      };
    }

    _render() {
      if (!this._config) return;
      const lang = languageOf(this._hass);
      const data = this._data();
      const choices = this._pcChoices();
      if (typeof customElements !== 'undefined' && customElements.get('ha-form')) {
        if (!this._form) {
          this.shadowRoot.innerHTML = '';
          this._form = document.createElement('ha-form');
          this._form.addEventListener('value-changed', (ev) => this._changed(ev.detail && ev.detail.value));
          this.shadowRoot.appendChild(this._form);
        }
        this._form.hass = this._hass;
        this._form.schema = [
          { name: 'title', selector: { text: {} } },
          {
            name: 'style',
            selector: {
              select: { mode: 'dropdown', options: STYLES.map((value) => ({ value, label: t(lang, `style_${value}`) })) },
            },
          },
          { name: 'show_images', selector: { boolean: {} } },
          { name: 'actions_when_pc_off', selector: { boolean: {} } },
          ...(choices.length
            ? [{ name: 'pc', selector: { select: { mode: 'dropdown', options: choices.map((value) => ({ value, label: value })) } } }]
            : []),
        ];
        this._form.computeLabel = (field) => t(languageOf(this._hass), `editor_${field.name}`);
        this._form.data = data;
        return;
      }
      if (!this._waiting && typeof customElements !== 'undefined') {
        this._waiting = true;
        customElements.whenDefined('ha-form').then(() => this._render());
      }
      this.shadowRoot.innerHTML = `
        <style>
          label { display: flex; align-items: center; gap: 12px; min-height: 44px; color: var(--primary-text-color); }
          input[type="text"], select { flex: 1; min-height: 36px; font: inherit; padding: 4px 8px; }
          input[type="checkbox"] { width: 20px; height: 20px; }
        </style>
        <label>${escapeHtml(t(lang, 'editor_title'))}
          <input type="text" name="title" value="${escapeHtml(data.title)}"></label>
        <label>${escapeHtml(t(lang, 'editor_style'))}
          <select name="style">${STYLES.map((value) => `<option value="${value}"${value === data.style ? ' selected' : ''}>
            ${escapeHtml(t(lang, `style_${value}`))}</option>`).join('')}</select></label>
        <label><input type="checkbox" name="show_images"${data.show_images ? ' checked' : ''}>
          ${escapeHtml(t(lang, 'editor_show_images'))}</label>
        <label><input type="checkbox" name="actions_when_pc_off"${data.actions_when_pc_off ? ' checked' : ''}>
          ${escapeHtml(t(lang, 'editor_actions_when_pc_off'))}</label>${choices.length ? `
        <label>${escapeHtml(t(lang, 'editor_pc'))}
          <select name="pc">${choices.map((value) => `<option value="${escapeHtml(value)}"${value === data.pc ? ' selected' : ''}>
            ${escapeHtml(value)}</option>`).join('')}</select></label>` : ''}`;
      this.shadowRoot.querySelectorAll('input, select').forEach((input) => {
        input.addEventListener('change', () => {
          const value = {};
          this.shadowRoot.querySelectorAll('input, select').forEach((i) => {
            value[i.name] = i.type === 'checkbox' ? i.checked : i.value;
          });
          this._changed(value);
        });
      });
    }

    _changed(value) {
      if (!value || !this._config) return;
      const config = { ...this._config };
      for (const field of EDITOR_FIELDS) {
        if (!(field in value)) continue;
        if (field === 'title') {
          const title = String(value.title || '').trim();
          if (title) config.title = String(value.title);
          else delete config.title;
        } else if (field === 'pc') {
          const pc = String(value.pc || '').trim();
          // the PC without a name is the default (with several PCs the one the card shows without pc)
          if (pc && pc.toLowerCase() !== UNNAMED_PC.toLowerCase()) config.pc = pc;
          else delete config.pc;
        } else if (field === 'style') {
          const style = normalizeStyle(value.style);
          if (style === STYLES[0]) delete config.style; // the default style is not written
          else config.style = style;
        } else if (value[field] === false) {
          config[field] = false;
        } else {
          delete config[field]; // on is the default
        }
      }
      this._config = config;
      this.dispatchEvent(new CustomEvent('config-changed', { detail: { config }, bubbles: true, composed: true }));
    }
  }

  if (!customElements.get(CARD_TYPE)) customElements.define(CARD_TYPE, SteamEngineCard);
  if (!customElements.get(EDITOR_TYPE)) customElements.define(EDITOR_TYPE, SteamEngineCardEditor);
}

if (typeof customElements !== 'undefined' && typeof HTMLElement !== 'undefined') {
  defineElements();
  if (typeof window !== 'undefined') {
    window.customCards = window.customCards || [];
    if (!window.customCards.some((c) => c && c.type === CARD_TYPE)) {
      const lang = typeof navigator !== 'undefined' && /^de\b/i.test(navigator.language || '') ? 'de' : 'en';
      window.customCards.push({
        type: CARD_TYPE,
        name: t(lang, 'card_name'),
        description: t(lang, 'card_description'),
        preview: true,
        documentationURL: 'https://github.com/dust51490-spec/steam-engine',
      });
    }
  }
}
