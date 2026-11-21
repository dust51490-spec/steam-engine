// Tests of the pure functions of the dashboard card (node --test, no npm packages).
// Importing the module must not need a browser: the custom elements are only defined there.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import * as card from '../../custom_components/steam_engine/frontend/steam-engine-card.js';

const state = (value, attributes = {}, extra = {}) => ({ state: value, attributes, ...extra });

test('the module imports without a browser and defines nothing', () => {
  assert.equal(typeof globalThis.customElements, 'undefined');
  assert.equal(card.CARD_TYPE, 'steam-engine-card');
});

test('English and German texts have the same keys and placeholders', () => {
  const placeholders = (text) => [...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort().join(',');
  const en = card.TEXTS.en;
  const de = card.TEXTS.de;
  assert.deepEqual(Object.keys(de).sort(), Object.keys(en).sort());
  for (const key of Object.keys(en)) {
    assert.equal(placeholders(de[key]), placeholders(en[key]), key);
  }
});

test('language and locale follow hass.locale', () => {
  assert.equal(card.languageOf({ locale: { language: 'de' } }), 'de');
  assert.equal(card.languageOf({ locale: { language: 'de-CH' } }), 'de');
  assert.equal(card.languageOf({ locale: { language: 'fr' } }), 'en');
  assert.equal(card.languageOf({ language: 'de' }), 'de');
  assert.equal(card.languageOf(undefined), 'en');
  assert.equal(card.localeOf({ locale: { language: 'not a locale!' } }), 'en');
});

test('texts are filled in and fall back to English', () => {
  assert.equal(card.t('de', 'msg_uninstalled', { name: 'Raft' }), 'Raft deinstalliert');
  assert.equal(card.t('fr', 'msg_uninstalled', { name: 'Raft' }), 'Raft uninstalled');
  assert.equal(card.t('en', 'no_such_key'), 'no_such_key');
  assert.equal(card.tn('en', 'updates_count', 1), '1 update');
  assert.equal(card.tn('de', 'updates_count', 3), '3 Updates');
});

test('reasons are translated, unknown codes shown as they are', () => {
  assert.equal(card.reasonText('de', 'game_running'), 'es läuft gerade ein Spiel');
  assert.equal(card.reasonText('en', 'agent_offline'), 'the Steam agent on the PC cannot be reached');
  assert.equal(card.reasonText('en', 'already_sent'), 'sent a moment ago already');
  assert.equal(card.reasonText('de', 'already_queued'), 'schon im laufenden Auftrag oder in der Warteschlange');
  assert.equal(card.reasonText('en', 'something_new'), 'something_new');
  assert.equal(card.reasonText('en', undefined), 'no answer from the PC');
});

test('sizes and dates are written as the locale does', () => {
  assert.equal(card.formatSize(3277, 'de'), '3,2\u00a0GB');
  assert.equal(card.formatSize(3277, 'en'), '3.2\u00a0GB');
  assert.equal(card.formatSize(500, 'en'), '500\u00a0MB');
  assert.equal(card.formatSize(0, 'en'), '');
  assert.equal(card.formatSize('x', 'en'), '');
  assert.equal(card.formatGb(123.4, 'en'), '123\u00a0GB');
  assert.equal(card.formatGb(12.34, 'de'), '12,3\u00a0GB');
  assert.equal(card.formatDate('2026-11-03', 'de'), '03.11.2026');
  assert.equal(card.formatDate('', 'de'), '');
  assert.equal(card.formatSince('', 'en'), '');
  assert.notEqual(card.formatSince('2026-11-03T12:00:00+00:00', 'en', new Date('2026-11-06T12:00:00Z')), '');
});

test('the configuration gets its defaults and refuses unknown sections', () => {
  const config = card.normalizeConfig({ type: 'custom:steam-engine-card' });
  assert.deepEqual(config.sections, ['status', 'pc', 'free_space', 'updates', 'installed', 'history', 'not_installed']);
  assert.equal(config.show_images, true);
  assert.equal(config.actions_when_pc_off, true);
  assert.equal(config.title, '');

  const own = card.normalizeConfig({ sections: ['installed', 'status', 'installed'], show_images: false, actions_when_pc_off: false, title: 'Steam' });
  assert.deepEqual(own.sections, ['installed', 'status']);
  assert.equal(own.show_images, false);
  assert.equal(own.actions_when_pc_off, false);
  assert.equal(own.title, 'Steam');

  assert.deepEqual(card.normalizeConfig({ sections: ['status', 'history'] }).sections, ['status', 'history']);
  assert.throws(() => card.normalizeConfig({ sections: ['status', 'other'] }));
  assert.throws(() => card.normalizeConfig({ sections: 'status' }));
  assert.throws(() => card.normalizeConfig(null));
});

test('one card per PC: the option pc goes to every command and action', () => {
  assert.equal(card.normalizeConfig({}).pc, '');
  assert.deepEqual(card.pcField(card.normalizeConfig({})), {});
  const kids = card.normalizeConfig({ pc: ' Kinder-PC ' });
  assert.equal(kids.pc, 'Kinder-PC');
  assert.deepEqual(card.pcField(kids), { pc: 'Kinder-PC' });

  assert.deepEqual(card.editorPcs({ pcs: [{ pc: '' }, { pc: 'Kinder-PC' }, { pc: 'Kinder-PC' }] }), ['', 'Kinder-PC']);
  assert.deepEqual(card.editorPcs(null), []);

  assert.equal(card.loadErrorText('en', { code: 'unknown_pc' }, kids), 'Steam Engine is not set up for the PC "Kinder-PC".');
  assert.equal(card.loadErrorText('de', { code: 'not_loaded' }, kids), 'Steam Engine ist nicht eingerichtet.');
  assert.match(card.loadErrorText('en', { code: 'pc_needed' }, {}), /several PCs/);
  assert.match(card.loadErrorText('en', { code: 'x', message: 'boom' }, {}), /boom/);
});

const tabModel = (updates = 2) => ({
  updates: Array.from({ length: updates }, (_, i) => ({ appid: String(i + 1), name: `G${i}` })),
  library: [{ appid: '1' }, { appid: '2' }, { appid: '3' }],
  notInstalled: { known: true, games: [{ appid: '9' }] },
});

test('status and PC are the top area, the lists are tabs in the order of sections', () => {
  const all = card.normalizeConfig({});
  assert.deepEqual(card.topSections(all), ['status', 'pc']);
  // Free space first, so the card does not open on the long installed list
  assert.deepEqual(card.cardTabs(all, tabModel()), ['free_space', 'updates', 'installed', 'history', 'not_installed']);
  // Updates only while there are any; Check for updates is then in the status area
  assert.deepEqual(card.cardTabs(all, tabModel(0)), ['free_space', 'installed', 'history', 'not_installed']);
  assert.equal(card.checkInStatus(all, tabModel(0)), true);
  assert.equal(card.checkInStatus(all, tabModel(2)), false);
  const own = card.normalizeConfig({ sections: ['history', 'pc', 'installed'] });
  assert.deepEqual(card.topSections(own), ['pc']);
  assert.deepEqual(card.cardTabs(own, tabModel()), ['history', 'installed']);
  assert.equal(card.checkInStatus(own, tabModel()), true);
  assert.deepEqual(card.cardTabs(card.normalizeConfig({ sections: ['status'] }), tabModel()), []);
  assert.deepEqual(card.cardTabs(all, null), ['free_space', 'installed', 'history', 'not_installed']);
  // An own order is kept, free space included
  const ownOrder = card.normalizeConfig({ sections: ['installed', 'free_space', 'updates'] });
  assert.deepEqual(card.cardTabs(ownOrder, tabModel()), ['installed', 'free_space', 'updates']);
});

test('the remembered tab is used while it is shown, otherwise the first', () => {
  const tabs = ['updates', 'installed', 'history'];
  assert.equal(card.selectedTab(tabs, 'history'), 'history');
  assert.equal(card.selectedTab(tabs, ''), 'updates');
  assert.equal(card.selectedTab(tabs, 'free_space'), 'updates');
  assert.equal(card.selectedTab(['installed'], 'updates'), 'installed');
  assert.equal(card.selectedTab([], 'updates'), '');
  // Default tabs: nothing or something invalid remembered opens free space
  const defaults = card.cardTabs(card.normalizeConfig({}), tabModel());
  assert.equal(card.selectedTab(defaults, ''), 'free_space');
  assert.equal(card.selectedTab(defaults, 'nonsense'), 'free_space');
  assert.equal(card.selectedTab(defaults, 'installed'), 'installed');
  // A remembered updates tab while there are none falls back to the first
  assert.equal(card.selectedTab(card.cardTabs(card.normalizeConfig({}), tabModel(0)), 'updates'), 'free_space');
});

test('arrow keys, Home and End move between the tabs', () => {
  const tabs = ['updates', 'installed', 'history'];
  assert.equal(card.nextTab(tabs, 'updates', 'ArrowRight'), 'installed');
  assert.equal(card.nextTab(tabs, 'history', 'ArrowRight'), 'updates');
  assert.equal(card.nextTab(tabs, 'updates', 'ArrowLeft'), 'history');
  assert.equal(card.nextTab(tabs, 'installed', 'ArrowDown'), 'history');
  assert.equal(card.nextTab(tabs, 'installed', 'ArrowUp'), 'updates');
  assert.equal(card.nextTab(tabs, 'installed', 'Home'), 'updates');
  assert.equal(card.nextTab(tabs, 'installed', 'End'), 'history');
  assert.equal(card.nextTab(tabs, 'installed', 'Enter'), '');
  assert.equal(card.nextTab([], 'installed', 'ArrowRight'), '');
});

test('the tabs count updates, installed and owned games', () => {
  const m = tabModel();
  assert.equal(card.tabCount('updates', m), '2');
  assert.equal(card.tabCount('installed', m), '3');
  assert.equal(card.tabCount('not_installed', m), '1');
  assert.equal(card.tabCount('history', m), '');
  assert.equal(card.tabCount('free_space', m), '');
  assert.equal(card.tabCount('installed', { ...m, library: null }), '');
  assert.equal(card.tabCount('not_installed', { ...m, notInstalled: { known: false, games: [] } }), '');
  assert.equal(card.tabCount('updates', null), '');
});

test('the library is sorted by name and skips entries without a numeric app ID', () => {
  const library = state('3', {
    games: [
      { appid: '20', name: 'beta', size_mb: 100, last_played: '2026-11-01' },
      { appid: 'x', name: 'broken' },
      { appid: 10, name: 'Alpha', size_mb: '2048', last_played: '', unavailable: true },
      { appid: '30', name: 'Gamma 10' },
      { appid: '31', name: 'Gamma 9' },
    ],
  });
  const games = card.libraryGames(library);
  assert.deepEqual(games.map((g) => g.appid), ['10', '20', '31', '30']);
  assert.equal(games[0].unavailable, true);
  assert.equal(games[0].size_mb, 2048);
  assert.equal(games[1].unavailable, false);
  assert.equal(card.libraryGames(state('unknown', {})), null);
  assert.equal(card.libraryGames(undefined), null);
});

test('updates are merged with the automatic updates', () => {
  const updates = state('2', {
    games: [
      { appid: '2', name: 'Zeta', size_mb: 10 },
      { appid: '1', name: 'Alpha', size_mb: 20 },
    ],
    auto_update: ['2', 99],
    check_running: false,
  });
  assert.deepEqual(card.updateGames(updates), [
    { appid: '1', name: 'Alpha', size_mb: 20, auto: false, muted: false },
    { appid: '2', name: 'Zeta', size_mb: 10, auto: true, muted: false },
  ]);
  assert.deepEqual([...card.autoUpdateSet(updates)], ['2', '99']);
  assert.deepEqual(card.updateGames(undefined), []);
});

test('owned games: a hint without Steam account data, new purchases marked', () => {
  assert.deepEqual(card.notInstalledGames(state('0', { games: [], owned: [], new: [] })), { known: false, games: [] });
  const owned = card.notInstalledGames(state('2', {
    games: [{ appid: '7', name: 'b' }, { appid: '5', name: 'A' }],
    owned: ['5', '7', '9'],
    new: ['7'],
  }));
  assert.equal(owned.known, true);
  assert.deepEqual(owned.games, [
    { appid: '5', name: 'A', new: false },
    { appid: '7', name: 'b', new: true },
  ]);
  assert.equal(card.notInstalledGames(state('0', { games: [], owned: ['5'] })).known, true);
});

test('the running game and the job', () => {
  assert.equal(card.currentGame(state('unknown', { appid: null })), null);
  const current = card.currentGame(state('Raft', { appid: '648800', started: '2026-11-06T10:00:00+00:00' }));
  assert.equal(current.appid, '648800');
  assert.equal(current.picture, 'https://cdn.cloudflare.steamstatic.com/steam/apps/648800/header.jpg');

  assert.equal(card.jobActive('downloading', {}), true);
  assert.equal(card.jobActive('error', { position: 1 }), true);
  assert.equal(card.jobActive('error', {}), false);
  assert.equal(card.jobActive('done', {}), false);

  const job = card.jobInfo(
    state('42', { job: ['1', '2'], queue: [3] }),
    state('downloading', { appid: '2', phase: 'verify', phase_percent: 55.6, position: 2, count: 2 }),
  );
  assert.deepEqual(job, {
    job: ['1', '2'], queue: ['3'], active: true, appid: '2', percent: 42, phase: 'verify',
    phasePercent: 56, position: 2, count: 2, show: true,
  });
  const idle = card.jobInfo(state('0', { job: [], queue: [] }), state('done', { ok: ['2'] }));
  assert.equal(idle.show, false);
  assert.equal(idle.appid, '');
});

test('the model reads the entity IDs given by steam_engine/entities', () => {
  const ids = {
    entities: {
      pc: 'switch.my_pc', updates: 'sensor.u', not_installed: 'sensor.n', download_progress: 'sensor.p',
      current_game: 'sensor.c', auto_install_purchases: 'switch.buy', pc_state: 'sensor.pcs',
    },
    agent: { library: 'sensor.lib', install_status: 'sensor.job', library_free: 'sensor.free' },
  };
  const states = {
    'switch.my_pc': state('on'),
    'sensor.pcs': state('shutting_down'),
    'sensor.u': state('1', { games: [{ appid: '1', name: 'One' }], auto_update: [], check_running: true }),
    'sensor.n': state('0', { games: [], owned: [] }),
    'sensor.p': state('0', { job: [], queue: ['5'] }),
    'sensor.c': state('One', { appid: '1' }),
    'switch.buy': state('off'),
    'sensor.lib': state('1', { games: [{ appid: '1', name: 'One', size_mb: 5 }] }),
    'sensor.job': state('done', {}),
    'sensor.free': state('120.5', {
      drives: [
        { drive: 'F:', free_gb: 120.5, total_gb: 931, library: 'F:\\SteamLibrary', default: true, bus: 'SATA', media: 'HDD' },
        { drive: 'D:', free_gb: 1595, total_gb: 1863, library: null, default: false, bus: 'usb', media: 'tape' },
      ],
    }),
  };
  const model = card.buildModel(states, ids);
  assert.equal(model.pcOn, true);
  assert.equal(model.pcPhase, 'shutting_down');
  assert.equal(model.playing, '1');
  assert.equal(model.checkRunning, true);
  assert.deepEqual([...model.open], ['5']);
  assert.equal(model.purchasesEntity, 'switch.buy');
  assert.equal(model.purchasesOn, false);
  assert.equal(model.free.free, 120.5);
  // Drives without a library are kept apart (the install dialog offers them, the tab Space shows
  // them muted); bus and media in lower case, an unknown media is ''
  assert.deepEqual(model.free.drives, [
    { label: 'F:', letter: 'f', path: 'F:\\SteamLibrary', free_gb: 120.5, total_gb: 931, default: true, bus: 'sata', media: 'hdd' },
  ]);
  assert.deepEqual(model.free.noLibrary, [
    { label: 'D:', letter: 'd', path: '', free_gb: 1595, total_gb: 1863, default: false, bus: 'usb', media: '' },
  ]);
  assert.equal(card.gameName('en', model.names, '1'), 'One');
  assert.equal(card.gameName('de', model.names, '77'), 'Spiel 77');

  // PC section: the agent's buttons and the busy states
  assert.equal(model.pcEntity, 'switch.my_pc');
  assert.equal(model.restartEntity, '');
  assert.equal(model.agentOnline, false);
  assert.equal(model.gameRunning, true);
  assert.equal(model.steamBusy, false);
  const withAgent = card.buildModel({
    ...states,
    'sensor.c': state('unknown', {}),
    'binary_sensor.online': state('on'),
    'binary_sensor.dl': state('on'),
    'binary_sensor.game': state('off'),
    'button.r': state('unknown'),
    'button.l': state('2026-01-01T00:00:00+00:00'),
  }, {
    entities: { ...ids.entities, steam_busy: 'binary_sensor.busy' },
    agent: {
      ...ids.agent, agent_online: 'binary_sensor.online', steam_downloading: 'binary_sensor.dl',
      game_running: 'binary_sensor.game', restart_button: 'button.r', lock_button: 'button.l',
    },
  });
  assert.equal(withAgent.agentOnline, true);
  assert.equal(withAgent.steamBusy, true);
  assert.equal(withAgent.gameRunning, false);
  assert.equal(withAgent.restartEntity, 'button.r');
  assert.equal(withAgent.lockEntity, 'button.l');
  // A button that is missing or unavailable (agent offline) counts as unknown: pressing it does nothing
  const deadButtons = card.buildModel({ ...states, 'button.r': state('unavailable') }, {
    entities: ids.entities,
    agent: { ...ids.agent, restart_button: 'button.r', lock_button: 'button.l' },
  });
  assert.equal(deadButtons.restartEntity, '');
  assert.equal(deadButtons.lockEntity, '');

  const empty = card.buildModel({}, ids);
  assert.equal(empty.pcOn, false);
  assert.equal(empty.pcPhase, '');
  assert.equal(empty.library, null);
  assert.equal(empty.free, null);
  assert.equal(empty.purchasesKnown, false);
  assert.equal(empty.autoOffKnown, false);
  assert.equal(empty.steamRunning, null);
  assert.equal(empty.safe, null);
  assert.equal(empty.network, null);
  assert.equal(empty.lastInput, '');
  assert.equal(empty.closeGamesEntity, '');
  assert.equal(empty.forceShutdownEntity, '');
  assert.deepEqual(empty.history, { known: false, entries: [] });
});

test('the card watches every entity the model reads (a change of one renders it again)', () => {
  const keys = (prefix) => new Proxy({}, {
    get: (_, key) => (typeof key === 'string' ? `${prefix}.${key}` : undefined),
  });
  const ids = { entities: keys('own'), agent: keys('agent') };
  const read = new Set();
  const states = new Proxy({}, {
    get: (_, id) => {
      if (typeof id === 'string') read.add(id);
      return undefined;
    },
  });
  card.buildModel(states, ids);
  const watched = new Set(card.watchedIds(ids));
  assert.ok(read.has('own.pc_state'));
  for (const id of read) assert.ok(watched.has(id), `${id} is read but not watched`);
});

test('the model reads the PC status, auto off, the forced shut down, close games and the history', () => {
  const ids = {
    entities: {
      pc: 'switch.pc', auto_off_enabled: 'switch.off', safe_to_shut_down: 'binary_sensor.safe',
      plugin_busy: 'binary_sensor.plugin', steam_busy: 'binary_sensor.busy', download_progress: 'sensor.p',
    },
    agent: {
      install_status: 'sensor.job', agent_online: 'binary_sensor.online', steam_running: 'binary_sensor.steam',
      steam_downloading: 'binary_sensor.dl', game_running: 'binary_sensor.game', last_input: 'sensor.input',
      network: 'sensor.net', history: 'sensor.history', shutdown_button: 'button.s', close_games_button: 'button.c',
    },
  };
  const states = {
    'switch.pc': state('on'),
    'switch.off': state('on'),
    'binary_sensor.safe': state('on', { safe_since: '2026-11-06T11:00:00+00:00', off_at: '2026-11-06T12:10:00+00:00' }),
    'binary_sensor.plugin': state('on', { busy_by: ['Backup'] }),
    'binary_sensor.busy': state('on'),
    'sensor.p': state('0', { job: [], queue: [] }),
    'sensor.job': state('done', {}),
    'binary_sensor.online': state('on'),
    'binary_sensor.steam': state('off'),
    'binary_sensor.dl': state('off'),
    'binary_sensor.game': state('on'),
    'sensor.input': state('2026-11-06T11:58:00+00:00'),
    'sensor.net': state('1234', { unit_of_measurement: 'B/s', adapter: 'Ethernet' }),
    'sensor.history': state('2026-11-05T10:00:00+00:00', {
      total: 1, entries: [{ time: '2026-11-05T10:00:00+00:00', appid: '1', name: 'One', kind: 'updated', source: 'ha' }],
    }),
    'button.s': state('unknown'),
    'button.c': state('unknown'),
  };
  const m = card.buildModel(states, ids);
  assert.equal(m.autoOffEntity, 'switch.off');
  assert.equal(m.autoOffOn, true);
  assert.equal(m.autoOffKnown, true);
  assert.equal(m.safe, true);
  assert.equal(m.offAt, '2026-11-06T12:10:00+00:00');
  assert.equal(m.pluginBusy, true);
  assert.deepEqual(m.pluginBusyBy, ['Backup']);
  assert.equal(m.steamRunning, false);
  // Steam busy without our job: Steam downloads by itself (or a job is being handed over)
  assert.equal(m.jobRunning, false);
  assert.equal(m.steamDownloading, true);
  assert.equal(m.gameRunning, true);
  assert.equal(m.lastInput, '2026-11-06T11:58:00+00:00');
  assert.deepEqual(m.network, { value: '1234', unit: 'B/s' });
  assert.equal(m.forceShutdownEntity, 'button.s');
  assert.equal(m.closeGamesEntity, 'button.c');
  assert.equal(m.history.entries[0].name, 'One');
  assert.equal(card.pcActions(m).close_games, true);
  assert.equal(card.pcActions(m).force_shutdown, true);

  // Agent offline: its buttons are unavailable and hidden, its sensors have no state
  const offline = card.buildModel({
    ...states,
    'binary_sensor.online': state('off'),
    'binary_sensor.steam': state('unavailable'),
    'sensor.input': state('unavailable'),
    'sensor.net': state('unavailable', { unit_of_measurement: 'B/s' }),
    'button.s': state('unavailable'),
    'button.c': state('unavailable'),
  }, ids);
  assert.equal(offline.steamRunning, null);
  assert.equal(offline.lastInput, '');
  assert.equal(offline.network, null);
  assert.equal(offline.forceShutdownEntity, '');
  assert.equal(offline.closeGamesEntity, '');
  assert.equal(card.pcActions(offline).close_games, false);
  assert.equal(card.pcActions(offline).force_shutdown, false);
});

test('which buttons a row shows', () => {
  const base = { playing: '', pcOn: true, actionsWhenPcOff: true, open: new Set() };
  const game = { appid: '1', unavailable: false };
  assert.deepEqual(card.rowActions({ ...base, list: 'installed', game }),
    { launch: true, uninstall: true, autoUpdate: true, update: false, install: false });
  // The running game: no start, no uninstall
  assert.deepEqual(card.rowActions({ ...base, list: 'installed', game, playing: '1' }),
    { launch: false, uninstall: false, autoUpdate: true, update: false, install: false });
  // A game on a missing drive: no controls at all
  assert.deepEqual(card.rowActions({ ...base, list: 'installed', game: { appid: '1', unavailable: true } }),
    { launch: false, uninstall: false, autoUpdate: false, update: false, install: false });
  // PC off: the actions stay unless switched off; auto update always stays
  assert.equal(card.rowActions({ ...base, pcOn: false, list: 'installed', game }).launch, true);
  const off = card.rowActions({ ...base, pcOn: false, actionsWhenPcOff: false, list: 'installed', game });
  assert.equal(off.launch, false);
  assert.equal(off.uninstall, false);
  assert.equal(off.autoUpdate, true);
  assert.equal(card.rowActions({ ...base, pcOn: false, actionsWhenPcOff: false, list: 'updates', game }).update, false);
  assert.equal(card.rowActions({ ...base, pcOn: false, actionsWhenPcOff: false, list: 'not_installed', game }).install, false);
  // Already in the job or the queue
  assert.equal(card.rowActions({ ...base, list: 'updates', game, open: new Set(['1']) }).update, false);
  assert.equal(card.rowActions({ ...base, list: 'not_installed', game, open: new Set(['1']) }).install, false);
  assert.equal(card.rowActions({ ...base, list: 'not_installed', game }).install, true);
});

test('the PC state: buttons on their way, look and failures', () => {
  assert.equal(card.pcPhaseOf({ state: 'starting' }), 'starting');
  assert.equal(card.pcPhaseOf({ state: 'unavailable' }), '');
  assert.equal(card.pcPhaseOf(undefined), '');
  assert.equal(card.pcActionPending({ pcPhase: 'starting' }, 'wake'), true);
  assert.equal(card.pcActionPending({ pcPhase: 'starting' }, 'shutdown'), false);
  assert.equal(card.pcActionPending({ pcPhase: 'shutting_down' }, 'force_shutdown'), true);
  assert.equal(card.pcActionPending({ pcPhase: 'restarting' }, 'restart'), true);
  assert.equal(card.pcActionPending({ pcPhase: 'off' }, 'wake'), false);
  assert.equal(card.pcActionPending(null, 'wake'), false);
  assert.deepEqual(card.pcLook({ pcPhase: 'starting', pcOn: false }),
    { tone: 'blue', icon: 'mdi:power', text: 'pc_starting', pulse: true });
  assert.equal(card.pcLook({ pcPhase: 'shutting_down', pcOn: true }).text, 'pc_shutting_down');
  assert.equal(card.pcLook({ pcPhase: 'on', pcOn: true }).tone, 'green');
  assert.equal(card.pcLook({ pcPhase: '', pcOn: false }).text, 'pc_off');
  assert.equal(card.phaseFailure('starting', 'off'), 'msg_phase_failed_starting');
  assert.equal(card.phaseFailure('starting', 'on'), '');
  assert.equal(card.phaseFailure('shutting_down', 'on'), 'msg_phase_failed_shutting_down');
  assert.equal(card.phaseFailure('shutting_down', 'off'), '');
  assert.equal(card.phaseFailure('restarting', 'off'), 'msg_phase_failed_restarting');
  assert.equal(card.phaseFailure('restarting', 'on'), '');
  assert.equal(card.phaseFailure('', 'off'), '');
  assert.equal(card.phaseFailure('starting', ''), '');
});

test('which buttons the PC section shows', () => {
  const all = {
    pcEntity: 'switch.pc', restartEntity: 'button.r', lockEntity: 'button.l',
    forceShutdownEntity: 'button.s', closeGamesEntity: 'button.c',
  };
  const none = { wake: false, shutdown: false, restart: false, lock: false, close_games: false, force_shutdown: false };
  // PC off, agent offline: only wake (actions_when_pc_off plays no part)
  assert.deepEqual(card.pcActions({ ...all, pcOn: false, agentOnline: false }), { ...none, wake: true });
  // PC on, agent online, no game: everything but wake and close games
  assert.deepEqual(card.pcActions({ ...all, pcOn: true, agentOnline: true }),
    { ...none, shutdown: true, restart: true, lock: true, force_shutdown: true });
  // A game runs: close games too
  assert.equal(card.pcActions({ ...all, pcOn: true, agentOnline: true, gameRunning: true }).close_games, true);
  // PC on (reachable) but agent offline: nothing, the agent's buttons are unavailable
  assert.deepEqual(card.pcActions({ ...all, pcOn: true, agentOnline: false, gameRunning: true }), none);
  // A core or agent without the buttons in steam_engine/entities (or a button that is missing or
  // unavailable): they are hidden
  assert.deepEqual(card.pcActions({ pcEntity: 'switch.pc', restartEntity: '', lockEntity: '', pcOn: true, agentOnline: true, gameRunning: true }),
    { ...none, shutdown: true });
  assert.deepEqual(card.pcActions({ pcEntity: '', pcOn: false, agentOnline: false }), none);
});

test('which PC actions ask first', () => {
  assert.equal(card.pcDialogAction('pc_shutdown'), 'shutdown');
  assert.equal(card.pcDialogAction('pc_restart'), 'restart');
  assert.equal(card.pcDialogAction('pc_force_shutdown'), 'force_shutdown');
  assert.equal(card.pcDialogAction('pc_close_games'), 'close_games');
  assert.equal(card.pcDialogAction('pc_lock'), '');
  assert.equal(card.pcDialogAction('pc_wake'), '');
  assert.equal(card.pcDialogAction('uninstall'), '');
  assert.equal(card.pcDialogAction(undefined), '');
});

test('PC actions warn about a running game and, for a restart, a busy Steam', () => {
  assert.deepEqual(card.pcWarnings('restart', { gameRunning: false, steamBusy: false }), []);
  assert.deepEqual(card.pcWarnings('restart', { gameRunning: true, steamBusy: true }), ['warn_game', 'warn_steam']);
  assert.deepEqual(card.pcWarnings('restart', { gameRunning: false, steamBusy: true }), ['warn_steam']);
  // A busy Steam keeps the PC on for a shut down; the dialog text says so
  assert.deepEqual(card.pcWarnings('shutdown', { gameRunning: false, steamBusy: true }), []);
  assert.deepEqual(card.pcWarnings('shutdown', { gameRunning: true, steamBusy: false }), ['warn_game']);
  assert.deepEqual(card.pcWarnings('shutdown', { gameRunning: false, jobRunning: true, pluginBusy: true }), []);
});

test('the forced shut down names everything it does not wait for', () => {
  assert.deepEqual(card.pcWarnings('force_shutdown', {}), []);
  assert.deepEqual(card.pcWarnings('force_shutdown', {
    gameRunning: true, steamBusy: true, jobRunning: true, steamDownloading: true, pluginBusy: true, pluginBusyBy: ['Backup'],
  }), ['warn_game', 'warn_force_job', 'warn_force_download', 'warn_plugin_named']);
  assert.deepEqual(card.pcWarnings('force_shutdown', { steamDownloading: true, pluginBusy: true, pluginBusyBy: [] }),
    ['warn_force_download', 'warn_plugin']);
  // Closing games warns about nothing: the text says that unsaved progress is lost
  assert.deepEqual(card.pcWarnings('close_games', { gameRunning: true }), []);

  const force = card.pcConfirm('en', 'force_shutdown', { jobRunning: true, pluginBusy: true, pluginBusyBy: ['Backup', 'Build'] });
  assert.equal(force.title, 'Shut down the PC anyway?');
  assert.equal(force.ok, 'Shut down anyway');
  assert.match(force.text, /even while Steam downloads, a job runs, a plugin needs the PC or a game runs/);
  assert.deepEqual(force.warnings, [
    'A Steam Engine job is running and will be interrupted.',
    'A plugin needs the PC: Backup, Build.',
  ]);
  const de = card.pcConfirm('de', 'force_shutdown', { steamDownloading: true });
  assert.equal(de.title, 'PC trotzdem herunterfahren?');
  assert.equal(de.warnings.length, 1);
  // The normal shut down and the restart keep their texts
  assert.equal(card.pcConfirm('en', 'restart', { gameRunning: true }).warnings[0], 'A game is running and will be closed.');
  assert.equal(card.pcConfirm('de', 'shutdown', {}).ok, 'Herunterfahren');
});

test('closing games names the running game, or all running games', () => {
  const named = card.pcConfirm('de', 'close_games', { gameRunning: true, current: { appid: '1', name: 'Raft' } });
  assert.equal(named.title, 'Raft beenden?');
  assert.equal(named.ok, 'Spiele beenden');
  assert.match(named.text, /Alle Spiele/);
  assert.deepEqual(named.warnings, []);
  // The agent sees a game the integration does not know by name
  assert.equal(card.pcConfirm('en', 'close_games', { gameRunning: true, current: null }).title, 'Close all running games?');
});

test('PC actions call the PC switch and the agent buttons', () => {
  const m = { pcEntity: 'switch.pc', restartEntity: 'button.r', lockEntity: '', forceShutdownEntity: 'button.s', closeGamesEntity: 'button.c' };
  assert.deepEqual(card.pcServiceCall('wake', m), { domain: 'switch', service: 'turn_on', target: { entity_id: 'switch.pc' } });
  assert.deepEqual(card.pcServiceCall('shutdown', m), { domain: 'switch', service: 'turn_off', target: { entity_id: 'switch.pc' } });
  assert.deepEqual(card.pcServiceCall('restart', m), { domain: 'button', service: 'press', target: { entity_id: 'button.r' } });
  // The forced shut down presses the agent's button, not the PC switch with its checks
  assert.deepEqual(card.pcServiceCall('force_shutdown', m), { domain: 'button', service: 'press', target: { entity_id: 'button.s' } });
  assert.deepEqual(card.pcServiceCall('close_games', m), { domain: 'button', service: 'press', target: { entity_id: 'button.c' } });
  assert.equal(card.pcServiceCall('close_games', { ...m, closeGamesEntity: '' }), null);
  assert.equal(card.pcServiceCall('lock', m), null);
  assert.equal(card.pcServiceCall('other', m), null);
});

test('relative times and data rates', () => {
  const now = new Date('2026-11-06T12:00:00Z');
  assert.equal(card.formatRelative('2026-11-06T11:55:00+00:00', 'en', now), '5 minutes ago');
  assert.equal(card.formatRelative('2026-11-06T11:55:00+00:00', 'de', now), 'vor 5 Minuten');
  assert.equal(card.formatRelative('2026-11-06T12:12:00Z', 'en', now), 'in 12 minutes');
  assert.equal(card.formatRelative('2026-11-06T09:00:00Z', 'en', now), '3 hours ago');
  assert.equal(card.formatRelative('2026-11-04T12:00:00Z', 'en', now), '2 days ago');
  assert.equal(card.formatRelative('2026-11-06T11:59:40Z', 'en', now), 'now');
  assert.equal(card.formatRelative('', 'en', now), '');
  assert.equal(card.formatRelative('nonsense', 'en', now), '');

  assert.equal(card.formatRate('0', 'B/s', 'en'), '0 B/s');
  assert.equal(card.formatRate('512', 'B/s', 'en'), '512 B/s');
  assert.equal(card.formatRate('1536', 'B/s', 'en'), '1.5 kB/s');
  assert.equal(card.formatRate('12345678', 'B/s', 'de'), '12,3 MB/s');
  assert.equal(card.formatRate('250000000', '', 'en'), '250 MB/s');
  // A unit chosen in Home Assistant stays
  assert.equal(card.formatRate('98.76', 'Mbit/s', 'en'), '98.8 Mbit/s');
  assert.equal(card.formatRate('unknown', 'B/s', 'en'), '');
  assert.equal(card.formatRate(null, 'B/s', 'en'), '');
});

test('the status lines of the PC section', () => {
  const now = new Date('2026-11-06T12:00:00Z');
  const online = {
    agentOnline: true, steamRunning: true, steamBusy: false, jobRunning: false, steamDownloading: false,
    lastInput: '2026-11-06T11:58:00+00:00', network: { value: '2048', unit: 'B/s' }, safe: false, offAt: '',
    pluginBusy: false, pluginBusyBy: [],
  };
  assert.deepEqual(card.pcFacts('en', online, 'en', now), [
    ['Steam', 'running'],
    ['Steam busy', 'no'],
    ['Last input', '2 minutes ago'],
    ['Network', '2 kB/s'],
    ['Safe to shut down', 'no'],
  ]);
  const busy = card.pcFacts('de', {
    ...online, steamBusy: true, jobRunning: true, safe: true, offAt: '2026-11-06T12:12:00+00:00',
    pluginBusy: true, pluginBusyBy: ['Backup'],
  }, 'de', now);
  assert.deepEqual(busy.map((f) => f[0]), [
    'Steam', 'Steam beschäftigt', 'Zuletzt aktiv', 'Netzwerk', 'Sicher zum Abschalten', 'Auto-Aus', 'Durch Plugin belegt',
  ]);
  assert.equal(busy[1][1], 'Auftrag läuft');
  assert.equal(busy[4][1], 'ja');
  assert.match(busy[5][1], /\(in 12 Minuten\)$/);
  assert.equal(busy[6][1], 'Backup');
  assert.equal(card.pcFacts('en', { ...online, jobRunning: false, steamDownloading: true, steamBusy: true }, 'en', now)[1][1], 'downloading');
  // Agent offline: the agent's lines are left out, the integration's stay
  assert.deepEqual(card.pcFacts('en', {
    agentOnline: false, steamRunning: null, steamBusy: false, lastInput: '', network: null, safe: false, offAt: '', pluginBusy: false,
  }, 'en', now), [['Safe to shut down', 'no']]);
});

test('the update history: newest first, at most ten', () => {
  assert.deepEqual(card.historyEntries(undefined), { known: false, entries: [] });
  assert.deepEqual(card.historyEntries(state('unknown', {})), { known: false, entries: [] });
  assert.deepEqual(card.historyEntries(state('2026-11-01T10:00:00+01:00', { total: 0, entries: [] })), { known: true, entries: [] });
  const history = card.historyEntries(state('2026-11-03T10:00:00+01:00', {
    total: 4,
    entries: [
      { time: '2026-11-01T10:00:00+01:00', appid: '1', name: 'Old', kind: 'installed', source: 'steam', old_build: '', new_build: '5' },
      { time: '2026-11-03T10:00:00+01:00', appid: '2', name: 'New', kind: 'updated', source: 'ha', old_build: '7', new_build: '8' },
      'broken',
      { time: 'no time', appid: '3', name: 'Undated', kind: 'removed', source: 'ha' },
      { time: '2026-11-02T10:00:00+01:00', appid: 'x', name: '', kind: 'updated' },
      { time: '2026-11-02T09:00:00Z', appid: 4, name: 'Middle', kind: 'updated', source: 'steam' },
    ],
  }));
  assert.equal(history.known, true);
  assert.deepEqual(history.entries.map((e) => e.name), ['New', 'Middle', 'Old', 'Undated']);
  assert.deepEqual(history.entries[0], {
    time: '2026-11-03T10:00:00+01:00', appid: '2', name: 'New', kind: 'updated', source: 'ha', oldBuild: '7', newBuild: '8',
  });
  assert.equal(history.entries[1].appid, '4');
  assert.equal(history.entries[2].oldBuild, '');

  const many = Array.from({ length: 30 }, (_, i) => ({
    time: `2026-10-${String(i + 1).padStart(2, '0')}T12:00:00+00:00`, appid: String(i + 1), name: `Game ${i + 1}`, kind: 'updated',
  }));
  const shown = card.historyEntries(state('x', { entries: many }));
  assert.equal(shown.entries.length, card.HISTORY_LIMIT);
  assert.equal(shown.entries[0].name, 'Game 30');
  assert.equal(card.historyEntries(state('x', { entries: many }), 3).entries.length, 3);
});

test('answers of the actions become messages with the game name', () => {
  assert.deepEqual(card.responseMessage('de', 'uninstall', 'Raft', { sent: true }),
    { type: 'success', text: 'Deinstallation von Raft gestartet', track: true });
  assert.deepEqual(card.responseMessage('de', 'install', 'Raft', { queued: true, sent: false }),
    { type: 'success', text: 'Raft in der Warteschlange', track: true });
  assert.deepEqual(card.responseMessage('en', 'update', 'Raft', { queued: false, sent: true, woken: false }),
    { type: 'success', text: 'Update of Raft started', track: true });
  assert.deepEqual(card.responseMessage('en', 'launch', 'Raft', { sent: true }),
    { type: 'success', text: 'Raft is starting', track: false });
  assert.deepEqual(card.responseMessage('en', 'launch', 'Raft', { sent: false, reason: 'game_running' }),
    { type: 'error', text: 'Raft: not done, a game is running', track: false });
  assert.deepEqual(card.responseMessage('de', 'uninstall', 'Raft', { sent: false, reason: 'already_sent' }),
    { type: 'error', text: 'Raft: nicht ausgeführt, wurde gerade erst gesendet', track: false });
  assert.deepEqual(card.responseMessage('en', 'install', 'Raft', { queued: false, sent: false, reason: 'already_queued' }),
    { type: 'error', text: 'Raft: not done, already in the running job or the queue', track: false });
  // Not handed over without a reason: the job waits for the agent
  const pending = card.responseMessage('en', 'install', 'Raft', { queued: false, sent: false, woken: true });
  assert.equal(pending.type, 'info');
  assert.equal(pending.track, true);
  assert.equal(card.responseMessage('en', 'auto_update', 'Raft', { appid: '1', auto_update: true }), null);
  assert.equal(card.responseMessage('en', 'auto_update', 'Raft', { appid: 'x', auto_update: null, reason: 'invalid_appid' }).type, 'error');
  assert.equal(card.responseMessage('en', 'check', '', { games: [{}, {}], skipped: false }).text, 'Sync done: 2 updates');
  assert.equal(card.responseMessage('de', 'check', '', { games: [], skipped: false }).text, 'Abgleich fertig: alles aktuell');
  assert.equal(
    card.responseMessage('de', 'check', '', { games: [{}], skipped: false, new_purchases: [{ appid: '1', name: 'A' }] }).text,
    'Abgleich fertig: 1 Update, 1 neuer Kauf',
  );
  assert.equal(card.responseMessage('en', 'check', '', { games: [], skipped: true }).type, 'info');
});

test('the end of a job is only taken when it came after the action', () => {
  const before = state('done', { ok: ['1'] });
  const mark = card.statusSignature(before);
  // The same end again (e.g. written again after an MQTT reconnect) is not the end of the new job
  assert.equal(card.jobOutcome('1', state('done', { ok: ['1'] }), mark), null);
  assert.equal(card.jobOutcome('1', state('downloading', { appid: '1', position: 1 }), mark), null);
  assert.equal(card.jobOutcome('1', state('done', { ok: ['1'], updated: 'later' }), mark), 'ok');
  assert.equal(card.jobOutcome('1', state('done_with_errors', { ok: [], failed: ['1'] }), mark), 'failed');
  assert.equal(card.jobOutcome('1', state('done_with_errors', { no_space: [{ appid: '1', needed_gb: 50 }] }), mark), 'no_space');
  assert.equal(card.jobOutcome('1', state('done_with_errors', { game_running: ['1'] }), mark), 'game_running');
  assert.equal(card.jobOutcome('1', state('done', { skipped: '1' }), mark), 'skipped');
  // The end of another job (ours was queued)
  assert.equal(card.jobOutcome('1', state('done', { ok: ['2'] }), mark), null);
  // aborted names no games: it only counts once the game's job was seen running
  assert.equal(card.jobOutcome('1', state('aborted', {}), mark), null);
  assert.equal(card.jobOutcome('1', state('aborted', {}), mark, true), 'aborted');
  assert.equal(card.jobOutcome('1', undefined, mark), null);
  // A job with the same result as the one before: its end equals the mark, the run tells it apart
  assert.equal(card.jobOutcome('1', state('done', { ok: ['1'] }), mark, true), 'ok');
});

test('a running job names the games it is about', () => {
  assert.equal(card.jobRunsFor('1', state('started', { appids: ['1', '2'], count: 2 })), true);
  assert.equal(card.jobRunsFor('3', state('started', { appids: ['1', '2'], count: 2 })), false);
  assert.equal(card.jobRunsFor('1', state('downloading', { appid: '1', position: 1 })), true);
  assert.equal(card.jobRunsFor('1', state('downloading', { appid: '2', position: 1 })), false);
  assert.equal(card.jobRunsFor('1', state('error', { appid: '1', position: 1 })), true);
  // Ends and states without a running job do not count
  assert.equal(card.jobRunsFor('1', state('error', { appid: '1' })), false);
  assert.equal(card.jobRunsFor('1', state('done', { ok: ['1'] })), false);
  assert.equal(card.jobRunsFor('1', undefined), false);
});

test('completion messages name the game', () => {
  const library = state('1', { games: [{ appid: '2', name: 'Other' }] });
  const uninstall = { action: 'uninstall', appid: '1', name: 'Raft', mark: '' };
  assert.deepEqual(card.completionMessage('de', uninstall, library, undefined), { type: 'success', text: 'Raft deinstalliert' });
  const stillThere = state('1', { games: [{ appid: '1', name: 'Raft' }] });
  assert.equal(card.completionMessage('de', uninstall, stillThere, undefined), null);
  // While the library is not known nothing is said
  assert.equal(card.completionMessage('de', uninstall, undefined, undefined), null);

  const mark = card.statusSignature(state('done', { ok: ['9'] }));
  const update = { action: 'update', appid: '1', name: 'Raft', mark };
  assert.deepEqual(card.completionMessage('en', update, library, state('done', { ok: ['1'] })), { type: 'success', text: 'Raft updated' });
  const install = { action: 'install', appid: '1', name: 'Raft', mark };
  assert.deepEqual(card.completionMessage('en', install, library, state('done', { ok: ['1'] })), { type: 'success', text: 'Raft installed' });
  assert.equal(card.completionMessage('en', install, library, state('done_with_errors', { failed: ['1'] })).type, 'error');
  assert.equal(card.completionMessage('en', install, library, state('done', { ok: ['9'] })), null);
  // The same end as at the tap, after the game's job was seen running
  const same = card.statusSignature(state('done', { ok: ['1'] }));
  assert.equal(card.completionMessage('en', { ...update, mark: same }, library, state('done', { ok: ['1'] })), null);
  assert.deepEqual(card.completionMessage('en', { ...update, mark: same, ran: true }, library, state('done', { ok: ['1'] })),
    { type: 'success', text: 'Raft updated' });
});

test('HTML is escaped and image URLs are built from the app ID', () => {
  assert.equal(card.escapeHtml('<a href="x">&\'</a>'), '&lt;a href=&quot;x&quot;&gt;&amp;&#39;&lt;/a&gt;');
  assert.equal(card.escapeHtml(undefined), '');
  assert.equal(card.pictureUrl('440'), 'https://cdn.cloudflare.steamstatic.com/steam/apps/440/header.jpg');
});

test('the status lines carry a key and mark what keeps the PC on', () => {
  const m = {
    agentOnline: true, steamRunning: true, steamBusy: false, jobRunning: false, steamDownloading: false,
    lastInput: '', network: null, safe: false, offAt: '', pluginBusy: true, pluginBusyBy: ['Backup'],
  };
  const items = card.pcFactItems('en', m, 'en');
  assert.deepEqual(items.map((f) => f.key), ['steam', 'steam_busy', 'safe', 'plugin']);
  assert.deepEqual(items.map((f) => f.warn), [false, false, true, true]);
  assert.deepEqual(card.pcFacts('en', m, 'en'), items.map((f) => [f.label, f.value]));
  assert.equal(card.pcFactItems('en', { ...m, safe: true, pluginBusy: false }, 'en').some((f) => f.warn), false);
});

test('one line names what keeps the PC from shutting down while the details are closed', () => {
  const m = { pcOn: true, safe: false, jobRunning: false, steamDownloading: false, pluginBusy: false, pluginBusyBy: [] };
  // Not safe only because of recent input: nothing holds the shut down, no line
  assert.equal(card.pcHold('en', m), '');
  assert.equal(card.pcHold('en', { ...m, steamDownloading: true }), 'Keeps the PC awake: Steam downloading');
  assert.equal(card.pcHold('en', { ...m, jobRunning: true, steamDownloading: true }), 'Keeps the PC awake: job running');
  assert.equal(card.pcHold('de', { ...m, pluginBusy: true, pluginBusyBy: ['Backup', 'ESPHome'] }), 'Hält den PC wach: Backup, ESPHome');
  assert.equal(card.pcHold('en', { ...m, steamDownloading: true, pluginBusy: true }), 'Keeps the PC awake: Steam downloading, a plugin');
  assert.equal(card.pcHold('en', { ...m, pcOn: false, steamDownloading: true }), '');
  assert.equal(card.pcHold('en', null), '');
});

test('the PC details are open only when remembered as open', () => {
  assert.equal(card.detailsOpen('1'), true);
  assert.equal(card.detailsOpen('0'), false);
  assert.equal(card.detailsOpen(''), false);
  assert.equal(card.detailsOpen('true'), false);
});

const chipModel = (extra = {}) => ({
  pcOn: true, updates: [{ appid: '1' }, { appid: '2' }], steamDownloading: false,
  job: { appid: '', percent: 0 }, free: { free: 412.4, drives: [] }, ...extra,
});

test('the status chips: PC, download, updates and free space', () => {
  const chips = card.statusChips('en', chipModel({ job: { appid: '7', percent: 63 } }), 'en');
  assert.deepEqual(chips.map((c) => [c.key, c.tone, c.text]), [
    ['pc', 'green', 'PC on'],
    ['download', 'blue', 'Downloading 63 %'],
    ['updates', 'blue', '2 updates'],
    ['free', 'neutral', '412 GB free'],
  ]);
  const idle = card.statusChips('de', chipModel({ pcOn: false, updates: [], free: null }), 'de');
  assert.deepEqual(idle.map((c) => [c.key, c.tone, c.text]), [
    ['pc', 'neutral', 'PC aus'],
    ['updates', 'green', 'alles aktuell'],
  ]);
  // The free space sensor keeps the last known values while the PC is off
  const offKnown = card.statusChips('de', chipModel({ pcOn: false, updates: [] }), 'de');
  assert.deepEqual(offKnown.map((c) => c.key), ['pc', 'updates', 'free']);
  const steam = card.statusChips('en', chipModel({ steamDownloading: true }), 'en');
  assert.equal(steam[1].text, 'Steam downloading');
  const starting = card.statusChips('de', chipModel({ pcOn: false, pcPhase: 'starting' }), 'de');
  assert.deepEqual([starting[0].tone, starting[0].text, starting[0].pulse], ['blue', 'PC startet …', true]);
});

const rowModel = {
  playing: '1',
  updates: [{ appid: '4' }],
  job: { active: true, appid: '2', percent: 63 },
  open: new Set(['2', '3']),
};

test('a row shows the state of its game as tags, without an icon', () => {
  const keys = (r) => r.tags.map((x) => x.key);
  const playing = card.rowState('installed', { appid: '1' }, rowModel);
  assert.equal(playing.tone, 'green');
  assert.deepEqual(keys(playing), ['badge_playing']);
  const running = card.rowState('installed', { appid: '2' }, rowModel);
  assert.equal(running.tone, 'blue');
  assert.deepEqual(running.tags, [{ key: 'tag_downloading', tone: 'blue', vars: undefined }]);
  const queued = card.rowState('installed', { appid: '3' }, rowModel);
  assert.equal('icon' in queued, false);
  assert.deepEqual(keys(queued), ['badge_queued']);
  const update = card.rowState('installed', { appid: '4' }, rowModel);
  assert.equal(update.tone, 'amber');
  assert.deepEqual(keys(update), ['tag_update']);
  const plain = card.rowState('installed', { appid: '5' }, rowModel);
  assert.equal(plain.tone, 'neutral');
  assert.deepEqual(plain.tags, []);
  const missing = card.rowState('installed', { appid: '1', unavailable: true }, rowModel);
  assert.deepEqual([missing.tone, missing.tags.length], ['neutral', 0]);

  const auto = card.rowState('updates', { appid: '4', auto: true }, rowModel);
  assert.equal(auto.tone, 'amber');
  assert.deepEqual(keys(auto), ['badge_auto']);
  assert.deepEqual(keys(card.rowState('updates', { appid: '3' }, rowModel)), ['badge_queued']);

  const fresh = card.rowState('not_installed', { appid: '8', new: true }, rowModel);
  assert.deepEqual([fresh.tone, keys(fresh)], ['blue', ['badge_new']]);
  assert.equal(card.rowState('not_installed', { appid: '9' }, rowModel).tone, 'neutral');
});

test('the confirmations: game picture, colours and the PC switched on for it', () => {
  const config = card.normalizeConfig({});
  const off = card.confirmContent('en', { action: 'uninstall', appid: '648800', name: 'Raft', size: 10035 }, { pcOn: false }, config, 'en');
  assert.equal(off.title, 'Uninstall Raft?');
  assert.equal(off.text, 'The game files (9.8 GB) will be deleted from the PC. The PC is switched on for it.');
  assert.deepEqual([off.tone, off.okTone, off.ok, off.cancel], ['red', 'danger', 'Uninstall', 'Cancel']);
  assert.equal(off.image, card.pictureUrl('648800'));
  const install = card.confirmContent('de', { action: 'install', appid: '1', name: 'Hades' }, { pcOn: true },
    card.normalizeConfig({ show_images: false }), 'de');
  assert.deepEqual([install.title, install.tone, install.okTone, install.image], ['Hades installieren?', 'blue', 'fill', '']);
  assert.equal(install.text, card.t('de', 'confirm_install_text'));
  const pc = card.confirmContent('en', { action: 'pc_restart' }, { gameRunning: true, steamBusy: false }, config, 'en');
  assert.deepEqual([pc.title, pc.ok, pc.tone, pc.image, pc.icon], ['Restart the PC?', 'Restart', 'red', '', card.PC_ICONS.restart]);
  assert.deepEqual(pc.warnings, ['A game is running and will be closed.']);
});

// --- library style ---

test('the style: library by default, tiles on request, anything else falls back to library', () => {
  assert.deepEqual(card.STYLES, ['library', 'tiles']);
  assert.equal(card.normalizeConfig({}).style, 'library');
  assert.equal(card.normalizeConfig({ style: 'tiles' }).style, 'tiles');
  assert.equal(card.normalizeConfig({ style: 'library' }).style, 'library');
  assert.equal(card.normalizeConfig({ style: 'Tiles' }).style, 'library');
  assert.equal(card.normalizeConfig({ style: 'grid' }).style, 'library');
  assert.equal(card.normalizeConfig({ style: null }).style, 'library');
  assert.equal(card.normalizeStyle(42), 'library');
  for (const style of card.STYLES) {
    assert.notEqual(card.t('en', `style_${style}`), `style_${style}`);
    assert.notEqual(card.t('de', `style_${style}`), `style_${style}`);
  }
});

test('capsule images and placeholder colours come from the app ID', () => {
  assert.equal(card.capsuleUrl('440'), 'https://cdn.cloudflare.steamstatic.com/steam/apps/440/capsule_231x87.jpg');
  assert.equal(card.capsuleUrl('a/b'), 'https://cdn.cloudflare.steamstatic.com/steam/apps/a%2Fb/capsule_231x87.jpg');
  const hue = card.placeholderHue('1517290');
  assert.equal(hue, card.placeholderHue('1517290'));
  assert.ok(Number.isInteger(hue) && hue >= 0 && hue < 360);
  assert.notEqual(card.placeholderHue('440'), card.placeholderHue('550'));
  assert.equal(card.placeholderHue(''), 0);
});

const libModel = () => ({
  pcOn: true,
  playing: '1',
  job: { active: true, appid: '2', percent: 40 },
  open: new Set(['2', '3']),
  updates: [{ appid: '4', name: 'Delta', size_mb: 2048, auto: true }, { appid: '3', name: 'Gamma', size_mb: 100 }],
  autoUpdate: new Set(['4', '5']),
  library: [
    { appid: '1', name: 'Alpha', size_mb: 512, last_played: '2026-11-06', unavailable: false },
    { appid: '2', name: 'Beta', size_mb: 1024, last_played: '', unavailable: false },
    { appid: '3', name: 'Gamma', size_mb: 3000, last_played: '', unavailable: false },
    { appid: '4', name: 'Delta', size_mb: 4096, last_played: '2026-10-01', unavailable: false },
    { appid: '5', name: 'Epsilon', size_mb: 5000, last_played: '', unavailable: false },
    { appid: '6', name: 'Zeta', size_mb: 6000, last_played: '', unavailable: true },
  ],
  notInstalled: { known: true, games: [{ appid: '8', name: 'Theta', new: true }, { appid: '9', name: '' }] },
  names: new Map([['9', 'Iota']]),
});

test('a capsule of the grid: state flags, meta line, auto mark and accessible name', () => {
  const m = libModel();
  const keys = (tile) => tile.flags.map((f) => f.key);
  const playing = card.gridTile('en', 'en', 'installed', m.library[0], m);
  assert.deepEqual(keys(playing), ['badge_playing']);
  assert.deepEqual(playing.flags[0], { key: 'badge_playing', tone: 'green', icon: 'mdi:play', text: 'playing' });
  assert.equal(playing.meta[0], '512 MB');
  assert.match(playing.meta[1], /^last played /);
  assert.equal(playing.update, false);
  // In the job: downloading, no percent (a progress tick must not change the grid)
  const running = card.gridTile('en', 'en', 'installed', m.library[1], m);
  assert.deepEqual(keys(running), ['tag_downloading']);
  assert.deepEqual(running.meta, ['1 GB', 'never played']);
  assert.equal(JSON.stringify(running).includes('40'), false);
  // Queued with an update: queued, no update flag, but still marked as having one
  const queued = card.gridTile('en', 'en', 'installed', m.library[2], m);
  assert.deepEqual(keys(queued), ['badge_queued']);
  assert.equal(queued.update, true);
  // An update, automatic
  const update = card.gridTile('en', 'en', 'installed', m.library[3], m);
  assert.deepEqual([keys(update), update.update, update.auto], [['tag_update'], true, true]);
  assert.equal(update.label, `Delta, update, 4 GB, last played ${card.formatDate('2026-10-01', 'en')}, Automatic updates`);
  // A missing drive: dimmed, no auto mark, no last played
  const off = card.gridTile('de', 'de', 'installed', m.library[5], m);
  assert.deepEqual([off.off, off.auto, off.update, keys(off)], [true, false, false, ['tag_drive_missing']]);
  assert.deepEqual(off.meta, ['5,9 GB']);
  // The updates list: every game has the update flag unless it is in the job or the queue
  const upd = card.gridTile('en', 'en', 'updates', m.updates[0], m);
  assert.deepEqual([keys(upd), upd.meta, upd.auto, upd.update], [['tag_update'], ['update 2 GB'], true, true]);
  assert.deepEqual(keys(card.gridTile('en', 'en', 'updates', m.updates[1], m)), ['badge_queued']);
  // Owned games: new purchases flagged, the name from the names map, no meta
  const fresh = card.gridTile('en', 'en', 'not_installed', m.notInstalled.games[0], m);
  assert.deepEqual([keys(fresh), fresh.meta, fresh.auto], [['badge_new'], [], false]);
  assert.equal(card.gridTile('en', 'en', 'not_installed', m.notInstalled.games[1], m).name, 'Iota');
});

test('the detail of a game merges the lists and knows when the game is gone', () => {
  const m = libModel();
  const delta = card.gameDetail('en', '4', m);
  assert.deepEqual([delta.name, !!delta.installed, !!delta.update, delta.owned, delta.auto, delta.known],
    ['Delta', true, true, null, true, true]);
  assert.deepEqual([card.gameDetail('en', '1', m).playing, card.gameDetail('en', '2', m).running, card.gameDetail('en', '3', m).queued],
    [true, true, true]);
  assert.equal(card.gameDetail('en', '2', m).queued, false);
  assert.equal(card.gameDetail('en', '9', m).name, 'Iota');
  assert.equal(card.gameDetail('en', '6', m).unavailable, true);
  const gone = card.gameDetail('en', '77', m);
  assert.deepEqual([gone.known, gone.name], [false, 'Game 77']);
  assert.equal(card.gameDetail('en', '1', { ...m, library: null }).installed, null);
});

test('the actions of a game sheet follow the state of the game', () => {
  const m = libModel();
  const config = card.normalizeConfig({ style: 'library' });
  const actions = (appid, model = m, cfg = config) => card.sheetActions(card.gameDetail('en', appid, model), model, cfg);
  assert.deepEqual(actions('5'), ['launch', 'auto', 'uninstall']);
  assert.deepEqual(actions('4'), ['launch', 'update', 'auto', 'uninstall']);
  // The running game: only its switch
  assert.deepEqual(actions('1'), ['auto']);
  // In the job or the queue: no update
  assert.deepEqual(actions('3'), ['launch', 'auto', 'uninstall']);
  // A missing drive: nothing
  assert.deepEqual(actions('6'), []);
  // Owned, not installed: install, not while queued
  assert.deepEqual(actions('8'), ['install']);
  assert.deepEqual(actions('8', { ...m, open: new Set(['8']) }), []);
  // PC off: the actions stay by default, only the switch without actions_when_pc_off
  const off = { ...m, pcOn: false };
  assert.deepEqual(actions('4', off), ['launch', 'update', 'auto', 'uninstall']);
  const noActions = card.normalizeConfig({ style: 'library', actions_when_pc_off: false });
  assert.deepEqual(actions('4', off, noActions), ['auto']);
  assert.deepEqual(actions('8', off, noActions), []);
  assert.deepEqual(actions('77'), []);
  assert.deepEqual(card.sheetActions(null, m, config), []);
});

test('the flags and info line of a game sheet', () => {
  const m = libModel();
  const keys = (appid) => card.sheetFlags('en', card.gameDetail('en', appid, m)).map((f) => f.key);
  assert.deepEqual(keys('1'), ['badge_playing']);
  assert.deepEqual(keys('2'), ['tag_downloading']);
  assert.deepEqual(keys('3'), ['badge_queued']);
  assert.deepEqual(keys('4'), ['tag_update']);
  assert.deepEqual(keys('6'), ['tag_drive_missing']);
  assert.deepEqual(keys('8'), ['badge_new', 'not_installed']);
  assert.deepEqual(keys('9'), ['not_installed']);
  assert.deepEqual(card.sheetMeta('en', 'en', card.gameDetail('en', '4', m)),
    ['4 GB', `last played ${card.formatDate('2026-10-01', 'en')}`, 'update 2 GB']);
  assert.deepEqual(card.sheetMeta('de', 'de', card.gameDetail('de', '6', m)), ['5,9 GB', 'Laufwerk nicht verfügbar']);
  assert.deepEqual(card.sheetMeta('en', 'en', card.gameDetail('en', '8', m)), []);
  for (const key of Object.keys(card.FLAG_ICONS)) assert.notEqual(card.t('de', key), key);
});

test('the install size of steam_engine/game_size: kept, written and known', () => {
  assert.deepEqual(card.gameSizeEntry({ appid: '286160', size_mb: 2743, download_mb: 1177 }), { size_mb: 2743, download_mb: 1177 });
  assert.deepEqual(card.gameSizeEntry({ appid: '331970', size_mb: 749, download_mb: null }), { size_mb: 749, download_mb: null });
  for (const unknown of [null, undefined, {}, { size_mb: null }, { size_mb: 0 }, { size_mb: 'x' }, { size_mb: -1 }]) {
    assert.deepEqual(card.gameSizeEntry(unknown), { size_mb: null, download_mb: null });
  }
  assert.equal(card.gameSizeText(undefined, 'en'), '');
  assert.equal(card.gameSizeText({ loading: true }, 'en'), '…');
  assert.equal(card.gameSizeText({ size_mb: 2743 }, 'en'), '2.7 GB');
  assert.equal(card.gameSizeText({ size_mb: 2743 }, 'de'), '2,7 GB');
  assert.equal(card.gameSizeText({ size_mb: 749 }, 'en'), '749 MB');
  assert.equal(card.gameSizeText({ size_mb: null, download_mb: null }, 'en'), '');
  assert.equal(card.knownGameSize({ size_mb: 2743 }), 2743);
  assert.equal(card.knownGameSize({ loading: true }), 0);
  assert.equal(card.knownGameSize({ size_mb: null }), 0);
  assert.equal(card.knownGameSize(undefined), 0);
});

test('an unknown install size is asked again after a while, a known one or a running request is not', () => {
  const now = 1_000_000_000;
  const retry = card.GAME_SIZE_RETRY_MS;
  assert.equal(card.needsGameSize(undefined, now), true);
  assert.equal(card.needsGameSize({ loading: true }, now), false);
  assert.equal(card.needsGameSize({ size_mb: 2743, download_mb: 1177, at: now - 10 * retry }, now), false);
  // An error or no size from the integration: kept for a while, then asked again
  assert.equal(card.needsGameSize({ size_mb: null, download_mb: null, at: now - 1000 }, now), false);
  assert.equal(card.needsGameSize({ size_mb: null, download_mb: null, at: now - retry }, now), true);
  assert.equal(card.needsGameSize({ size_mb: null, download_mb: null }, now), true);
});

test('the sheet shows the install size right of the name for a game that is not installed only', () => {
  const m = libModel();
  const sizes = new Map([['8', { size_mb: 2743, download_mb: 1177 }], ['9', { loading: true }], ['5', { size_mb: 99 }]]);
  const view = {
    lang: 'en', locale: 'en', m, config: card.normalizeConfig({ style: 'library', show_images: false }),
    busy: () => false, failed: () => false, gameSize: (appid) => sizes.get(String(appid)),
  };
  const sheet = (appid, v = view) => {
    const d = card.gameDetail('en', appid, m);
    return card.RENDERERS.library.sheet(v, d, card.sheetActions(d, m, v.config));
  };
  const title = (html) => /<div class="shn">([\s\S]*?)<\/div>/.exec(html)[1];
  assert.equal(card.wantsGameSize(card.gameDetail('en', '8', m)), true);
  assert.equal(card.wantsGameSize(card.gameDetail('en', '5', m)), false);
  assert.equal(card.wantsGameSize(card.gameDetail('en', '4', m)), false);
  assert.equal(card.wantsGameSize(card.gameDetail('en', '77', m)), false);
  assert.equal(card.wantsGameSize(null), false);
  // Known: the size after the name, with its title
  assert.equal(title(sheet('8')),
    '<h2 id="sheet-title">Theta</h2><span class="gsize" data-k="size" title="Size of the installation">2.7 GB</span>');
  // Asked: an ellipsis
  assert.match(title(sheet('9')), /<h2 id="sheet-title">Iota<\/h2><span class="gsize"[^>]*>…<\/span>$/);
  // Unknown or not asked: the name alone
  assert.equal(title(sheet('8', { ...view, gameSize: () => ({ size_mb: null, download_mb: null }) })), '<h2 id="sheet-title">Theta</h2>');
  assert.equal(title(sheet('8', { ...view, gameSize: () => undefined })), '<h2 id="sheet-title">Theta</h2>');
  // Installed games keep their info line, no size after the name (also when one is known)
  const installed = sheet('5');
  assert.equal(title(installed), '<h2 id="sheet-title">Epsilon</h2>');
  assert.match(installed, /4\.9 GB/);
  assert.doesNotMatch(installed, /class="gsize"/);
});

test('the install dialog names the install size when it is known', () => {
  const config = card.normalizeConfig({});
  const request = { action: 'install', appid: '286160', name: 'Tabletop Simulator' };
  assert.equal(card.confirmContent('en', { ...request, size: 2743 }, { pcOn: true }, config, 'en').text,
    'About 2.7 GB will be installed on the PC.');
  assert.equal(card.confirmContent('de', { ...request, size: 2743 }, { pcOn: false }, config, 'de').text,
    'Etwa 2,7 GB werden auf dem PC installiert. Der PC wird dafür eingeschaltet.');
  assert.equal(card.confirmContent('en', { ...request, size: 749 }, { pcOn: true }, config, 'en').text,
    'About 749 MB will be installed on the PC.');
  // Unknown (still asked, no answer): the general text
  for (const size of [0, undefined, null]) {
    assert.equal(card.confirmContent('en', { ...request, size }, { pcOn: true }, config, 'en').text, card.t('en', 'confirm_install_text'));
  }
});

// --- active hours ---

test('the active hours and the waiting work come from the updates sensor', () => {
  assert.equal(card.activeHours(state('0', {})), null);
  assert.equal(card.activeHours(state('0', { active_hours: null })), null);
  assert.equal(card.activeHours(state('0', { active_hours: { start: '25:00', end: '20:00' } })), null);
  assert.equal(card.activeHours(state('0', { active_hours: ['08:00', '20:00'] })), null);
  assert.deepEqual(card.activeHours(state('0', { active_hours: { start: '08:00', end: '20:00' } })), { start: '08:00', end: '20:00' });
  // Seconds are dropped, a short hour is padded
  assert.deepEqual(card.activeHours(state('0', { active_hours: { start: '8:00', end: '06:30:00' } })), { start: '08:00', end: '06:30' });

  assert.deepEqual(card.deferredWork(state('0', {})), { until: '', appids: new Set() });
  assert.deepEqual(card.deferredWork(state('0', { deferred_until: 'soon', deferred: [] })), { until: '', appids: new Set() });
  const waiting = card.deferredWork(state('0', { deferred_until: '2026-11-07T08:00:00+01:00', deferred: ['10', 20, 'x'] }));
  assert.equal(waiting.until, '2026-11-07T08:00:00+01:00');
  assert.deepEqual([...waiting.appids], ['10', '20']);
});

test('the active hours and their next start are written as the locale does', () => {
  assert.equal(card.formatClock('08:00', 'de'), '08:00');
  assert.match(card.formatClock('20:00', 'en'), /^8:00\sPM$/);
  assert.equal(card.formatClock('24:00', 'de'), '');
  assert.equal(card.formatWindow('de', { start: '08:00', end: '20:00' }, 'de'), '08:00 – 20:00');
  assert.equal(card.formatWindow('de', { start: '22:00', end: '06:00' }, 'de'), '22:00 – 06:00');
  assert.equal(card.formatWindow('en', { start: '08:00', end: '08:00' }, 'en'), 'all day');
  assert.equal(card.formatWindow('en', null, 'en'), '');

  // Local times, so the test holds in every time zone
  const now = new Date(2026, 10, 6, 22, 0);
  const at = (day, hour) => new Date(2026, 10, day, hour, 0).toISOString();
  assert.equal(card.formatDeferredStart('de', at(6, 23), 'de', now), 'startet um 23:00');
  assert.equal(card.formatDeferredStart('de', at(7, 8), 'de', now), 'startet morgen um 08:00');
  assert.match(card.formatDeferredStart('en', at(7, 8), 'en', now), /^starts tomorrow at 8:00\sAM$/);
  assert.match(card.formatDeferredStart('de', at(9, 8), 'de', now), /^startet am Mo\., 9\. Nov\. um 08:00$/);
  assert.equal(card.formatDeferredStart('en', 'never', 'en', now), '');
  assert.equal(card.formatDeferredStart('en', '', 'en', now), '');

  // Rendered again just after midnight
  assert.equal(card.msToNextDay(now), 2 * 3600 * 1000 + 1000);
  assert.equal(card.msToNextDay(new Date(2026, 10, 6, 23, 59, 59, 900)), 1100);
});

test('the next start is shown in the time zone of Home Assistant, like the active hours', () => {
  // 22:00 in Berlin; the window opens at 08:00 Berlin time
  const now = new Date('2026-11-06T21:00:00Z');
  const until = '2026-11-07T08:00:00+01:00';
  assert.equal(card.formatDeferredStart('de', until, 'de', now, 'Europe/Berlin'), 'startet morgen um 08:00');
  assert.equal(card.formatDeferredStart('de', until, 'de', now, 'Europe/London'), 'startet morgen um 07:00');
  // The day is counted in that zone too: 16:00 in New York, the start is at 02:00 there tomorrow
  assert.equal(card.formatDeferredStart('de', until, 'de', now, 'America/New_York'), 'startet morgen um 02:00');
  // An unknown zone falls back to the browser's
  assert.equal(
    card.formatDeferredStart('de', until, 'de', now, 'No/Zone'),
    card.formatDeferredStart('de', until, 'de', now),
  );

  // Rendered again just after midnight there, also on the days of 25 and 23 hours (DST)
  assert.equal(card.msToNextDay(now, 'Europe/Berlin'), 2 * 3600 * 1000 + 1000);
  assert.equal(card.msToNextDay(new Date('2026-10-24T22:30:00Z'), 'Europe/Berlin'), 24.5 * 3600 * 1000 + 1000);
  assert.equal(card.msToNextDay(new Date('2027-03-27T23:30:00Z'), 'Europe/Berlin'), 22.5 * 3600 * 1000 + 1000);

  // The model carries the zone to the hint
  const m = { deferredUntil: until, deferred: new Set(), names: {}, timeZone: 'Europe/London' };
  assert.equal(card.deferredHint('de', m, 'de', now).text, 'Wartet auf Aktivitätszeit – startet morgen um 07:00');
  assert.equal(card.buildModel({}, {}, 'Europe/Berlin').timeZone, 'Europe/Berlin');
  assert.equal(card.buildModel({}, {}).timeZone, '');
});

const deferredIds = {
  entities: { pc: 'switch.pc', updates: 'sensor.u', not_installed: 'sensor.n', download_progress: 'sensor.p' },
  agent: { library: 'sensor.lib', install_status: 'sensor.job' },
};
const deferredStates = (updates, progress = { job: [], queue: [] }) => ({
  'switch.pc': state('off'),
  'sensor.u': state('2', {
    games: [{ appid: '4', name: 'Delta' }, { appid: '5', name: 'Epsilon' }], auto_update: ['4', '5'], ...updates,
  }),
  'sensor.n': state('1', { games: [{ appid: '8', name: 'Theta' }], owned: [8], new: ['8'] }),
  'sensor.p': state('0', progress),
  'sensor.lib': state('2', { games: [{ appid: '4', name: 'Delta' }, { appid: '5', name: 'Epsilon' }] }),
  'sensor.job': state('done', {}),
});

test('the model: waiting games leave the queue, manual actions stay', () => {
  const off = card.buildModel(deferredStates({ active_hours: null, deferred_until: null, deferred: [] }), deferredIds);
  assert.equal(off.activeHours, null);
  assert.equal(off.deferredUntil, '');
  assert.deepEqual([...off.deferred], []);
  assert.equal(card.deferredHint('en', off, 'en'), null);

  const until = new Date(2026, 10, 7, 8, 0).toISOString();
  const m = card.buildModel(deferredStates(
    { active_hours: { start: '08:00', end: '20:00' }, deferred_until: until, deferred: ['4', '8', '5'] },
    { job: ['5'], queue: ['4', '8', '9'] },
  ), deferredIds);
  assert.deepEqual(m.activeHours, { start: '08:00', end: '20:00' });
  assert.equal(m.deferredUntil, until);
  // 5 was handed to the PC: it is not waiting any more
  assert.deepEqual([...m.deferred], ['4', '8']);
  assert.deepEqual(m.job.queue, ['9']);
  assert.deepEqual([...m.open], ['5', '9']);
  // A tap on Update or Install starts a waiting game at once, also while the PC is off
  assert.equal(card.rowActions({ list: 'updates', game: m.updates[0], pcOn: false, open: m.open }).update, true);
  assert.equal(card.rowActions({ list: 'not_installed', game: m.notInstalled.games[0], pcOn: false, open: m.open }).install, true);

  const hint = card.deferredHint('de', m, 'de', new Date(2026, 10, 6, 22, 0));
  assert.deepEqual(hint, { text: 'Wartet auf Aktivitätszeit – startet morgen um 08:00', names: 'Delta, Theta' });
  // Without a valid start time: the hint still says that games wait
  assert.deepEqual(card.deferredHint('en', { ...m, deferredUntil: '' }, 'en'), { text: 'Waiting for active hours', names: 'Delta, Theta' });
  // A waiting time without games (e.g. the automatic retry) shows the time alone
  assert.equal(card.deferredHint('en', { ...m, deferred: new Set() }, 'en', new Date(2026, 10, 7, 1, 0)).names, '');
  // The configured window is a line of the PC details
  const facts = card.pcFacts('de', { ...m, steamRunning: null, safe: null, network: null }, 'de');
  assert.deepEqual(facts, [['Aktivitätszeit', '08:00 – 20:00']]);
});

test('a waiting game is marked in the rows, the grid and its sheet', () => {
  const m = { ...libModel(), deferred: new Set(['4', '8', '2']) };
  const keys = (list) => list.map((x) => x.key);
  // Tiles: waiting instead of the update tag; the updates list keeps its auto mark
  assert.deepEqual(keys(card.rowState('installed', { appid: '4' }, m).tags), ['badge_deferred']);
  const row = card.rowState('updates', { appid: '4', auto: true }, m);
  assert.deepEqual([row.tone, keys(row.tags)], ['amber', ['badge_deferred', 'badge_auto']]);
  assert.deepEqual(keys(card.rowState('not_installed', { appid: '8', new: true }, m).tags), ['badge_deferred', 'badge_new']);
  // The running game is not waiting
  assert.deepEqual(keys(card.rowState('installed', { appid: '2' }, m).tags), ['tag_downloading']);
  // Library: the flag with its icon, no second update flag
  const tile = card.gridTile('de', 'de', 'updates', m.updates[0], m);
  assert.deepEqual(tile.flags, [{ key: 'badge_deferred', tone: 'neutral', icon: 'mdi:clock-outline', text: 'wartet' }]);
  assert.equal(tile.update, true);
  assert.match(card.gridTile('en', 'en', 'installed', m.library[3], m).label, /^Delta, waiting, /);
  // Sheet: waiting, and Update stays
  const detail = card.gameDetail('en', '4', m);
  assert.deepEqual([detail.deferred, detail.queued], [true, false]);
  assert.deepEqual(keys(card.sheetFlags('en', detail)), ['badge_deferred']);
  assert.deepEqual(card.sheetActions(detail, m, card.normalizeConfig({})), ['launch', 'update', 'auto', 'uninstall']);
  assert.deepEqual(keys(card.sheetFlags('en', card.gameDetail('en', '8', m))), ['badge_deferred', 'badge_new', 'not_installed']);
  assert.equal(card.gameDetail('en', '2', m).deferred, false);
  // Models without the attributes mark nothing
  assert.equal(card.gameDetail('en', '4', libModel()).deferred, false);
});

// --- resolved images (steam_engine/images) ---

const BF6_HEADER = 'https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/2807960/d7ae078e0a543a97ff5a5d72165999535b0b1cdc/header.jpg?t=1793314012';
const BF6_CAPSULE = 'https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/2807960/d7ae078e0a543a97ff5a5d72165999535b0b1cdc/capsule_231x87.jpg?t=1793314012';

test('resolved images: only valid entries with https URLs or paths of Home Assistant are kept', () => {
  assert.deepEqual(card.normalizeImages(undefined), {});
  assert.deepEqual(card.normalizeImages({ images: [] }), {});
  assert.deepEqual(card.normalizeImages({}), {});
  const images = card.normalizeImages({
    images: {
      2807960: { header: ` ${BF6_HEADER} `, capsule: BF6_CAPSULE },
      440: { header: '/api/steam_engine/440.jpg', capsule: 7 },
      550: { header: 'http://example.com/a.jpg', capsule: '//evil.example/b.jpg' },
      660: { header: 'javascript:alert(1)' },
      abc: { header: BF6_HEADER },
      __proto__: { header: BF6_HEADER },
      770: null,
      880: { header: 'https://a b' },
    },
  });
  assert.deepEqual(Object.keys(images).sort(), ['2807960', '440']);
  assert.deepEqual(images['2807960'], { header: BF6_HEADER, capsule: BF6_CAPSULE });
  assert.deepEqual(images['440'], { header: '/api/steam_engine/440.jpg', capsule: '' });
});

test('resolved images: the resolved URL wins, the CDN path is the fallback', () => {
  const images = card.normalizeImages({ images: { 2807960: { header: BF6_HEADER, capsule: BF6_CAPSULE }, 1: { header: BF6_HEADER } } });
  assert.equal(card.steamImageUrl(images, '2807960', 'header'), BF6_HEADER);
  assert.equal(card.steamImageUrl(images, 2807960, 'capsule'), BF6_CAPSULE);
  assert.equal(card.steamImageUrl(images, '2807960'), BF6_HEADER);
  // Only one size resolved: the other is still better than Steam's grey placeholder
  assert.equal(card.steamImageUrl(images, '1', 'capsule'), BF6_HEADER);
  // Not resolved yet: the old paths, or the given fallback
  assert.equal(card.steamImageUrl(images, '440', 'header'), card.pictureUrl('440'));
  assert.equal(card.steamImageUrl(images, '440', 'capsule'), card.capsuleUrl('440'));
  assert.equal(card.steamImageUrl({}, '440', 'header', '/local/raft.jpg'), '/local/raft.jpg');
  assert.equal(card.steamImageUrl(null, '440', 'capsule'), card.capsuleUrl('440'));
  assert.equal(card.steamImageUrl(images, 'toString', 'header'), card.pictureUrl('toString'));
});

test('resolved images: the signature changes exactly with the URLs', () => {
  const a = card.normalizeImages({ images: { 2: { header: BF6_HEADER }, 1: { capsule: BF6_CAPSULE } } });
  const b = card.normalizeImages({ images: { 1: { capsule: BF6_CAPSULE }, 2: { header: BF6_HEADER } } });
  assert.equal(card.imagesSignature(a), card.imagesSignature(b));
  assert.equal(card.imagesSignature({}), '');
  assert.equal(card.imagesSignature(undefined), '');
  const c = card.normalizeImages({ images: { 1: { capsule: BF6_CAPSULE }, 2: { header: BF6_HEADER, capsule: BF6_CAPSULE } } });
  assert.notEqual(card.imagesSignature(a), card.imagesSignature(c));
});

test('resolved images: the model, the running game and the confirmations use them', () => {
  const ids = { entities: { current_game: 'sensor.cur', updates: 'sensor.upd' }, agent: { library: 'sensor.lib' } };
  const states = {
    'sensor.cur': state('Battlefield 6', { appid: '2807960', entity_picture: 'https://cdn.cloudflare.steamstatic.com/steam/apps/2807960/header.jpg' }),
    'sensor.lib': state('2', { games: [{ appid: '2807960', name: 'Battlefield 6' }, { appid: '440', name: 'TF2' }] }),
    'sensor.upd': state('0', { games: [{ appid: '550', name: 'L4D2' }] }),
  };
  const images = card.normalizeImages({ images: { 2807960: { header: BF6_HEADER, capsule: BF6_CAPSULE } } });
  const m = card.buildModel(states, ids, '', images);
  assert.equal(m.current.picture, BF6_HEADER);
  assert.equal(m.images, images);
  // Without resolved images the sensor's picture stays
  const plain = card.buildModel(states, ids);
  assert.equal(plain.current.picture, 'https://cdn.cloudflare.steamstatic.com/steam/apps/2807960/header.jpg');
  assert.deepEqual(plain.images, {});
  const config = card.normalizeConfig({});
  const request = { action: 'uninstall', appid: '2807960', name: 'Battlefield 6', size: 1000 };
  assert.equal(card.confirmContent('en', request, m, config, 'en').image, BF6_HEADER);
  assert.equal(card.confirmContent('en', request, plain, config, 'en').image, card.pictureUrl('2807960'));
});

test('resolved images: a refused subscription is tried again a few times, then on a reconnect', () => {
  assert.deepEqual(card.IMAGES_RETRY_MS, [5000, 15000, 60000]);
  assert.equal(card.imagesRetryDelay(0), 5000);
  assert.equal(card.imagesRetryDelay(1), 15000);
  assert.equal(card.imagesRetryDelay(2), 60000);
  // No poll: after the last wait the card waits for a reconnect or the integration's entities
  assert.equal(card.imagesRetryDelay(3), 0);
  assert.equal(card.imagesRetryDelay(100), 0);
  assert.equal(card.imagesRetryDelay(-1), 0);
  assert.equal(card.imagesRetryDelay(undefined), 0);
});

test("resolved images: the card subscribes again when the integration's entities appear", () => {
  // Home Assistant restarted and loaded the integration after the tries ran out: its entities
  // were missing and are back, no subscription runs
  assert.equal(card.imagesResubscribeDue(true, false, false), true);
  // A subscription runs or is being made: nothing
  assert.equal(card.imagesResubscribeDue(true, false, true), false);
  // Still missing, or present all along (the usual state update): nothing
  assert.equal(card.imagesResubscribeDue(true, true, false), false);
  assert.equal(card.imagesResubscribeDue(false, false, false), false);
  assert.equal(card.imagesResubscribeDue(false, true, false), false);
  assert.equal(card.imagesResubscribeDue(undefined, false, false), false);
});

// --- target drive of a first install ---

const freeState = (drives) => state('1179', { drives });
const F = { drive: 'F:', free_gb: 1179, total_gb: 1863, library: 'F:\\SteamLibrary', default: true };
const C = { drive: 'C:', free_gb: 360, total_gb: 476, library: 'C:\\Program Files (x86)\\Steam', default: false };
const D = { drive: 'D:', free_gb: 1595, total_gb: 1863, library: null, default: false };

test('the install dialog offers the drives with a library, then those without, from two on', () => {
  const config = card.normalizeConfig({ show_images: false });
  const request = { action: 'install', appid: '440', name: 'TF2' };
  // One drive with a library and nothing else: no choice, nothing to send
  const one = { pcOn: true, free: card.freeSpace(freeState([F])) };
  assert.deepEqual(card.driveChoices(one), []);
  const plain = card.confirmContent('en', request, one, config, 'en');
  assert.deepEqual([plain.drives, plain.driveLabel], [[], '']);
  // No data, an old agent without drives, a model without free space or without noLibrary
  assert.deepEqual(card.driveChoices({ free: card.freeSpace(state('5', {})) }), []);
  assert.deepEqual(card.driveChoices({ free: null }), []);
  assert.deepEqual(card.driveChoices(undefined), []);
  assert.deepEqual(card.driveChoices({ free: { free: 5, drives: [{ ...F, letter: 'f' }] } }), []);
  assert.deepEqual(card.confirmContent('en', request, { pcOn: true }, config, 'en').drives, []);

  // One library and one drive without: a choice, the empty drive with its hint
  const withEmpty = { pcOn: true, free: card.freeSpace(freeState([F, D])) };
  assert.deepEqual(card.driveChoices(withEmpty).map((d) => [d.letter, d.newLibrary]), [['f', false], ['d', true]]);
  const e = card.confirmContent('en', request, withEmpty, config, 'en');
  assert.equal(e.driveLabel, 'Install to');
  assert.deepEqual(e.drives, [
    { letter: 'f', label: 'F:', free: '1,179\u00a0GB free', note: '', usb: false, media: '', fits: null, fit: '', default: true, checked: true },
    { letter: 'd', label: 'D:', free: '1,595\u00a0GB free', note: 'new Steam library', usb: false, media: '', fits: null, fit: '', default: false, checked: false },
  ]);
  assert.deepEqual(card.confirmContent('de', request, withEmpty, config, 'de').drives.map((d) => d.note), ['', 'neue Steam-Bibliothek']);
  // Only drives without a library (Steam's folder holds the only library, but on no letter): a choice too
  const empties = { pcOn: true, free: card.freeSpace(freeState([D, { ...D, drive: 'E:', free_gb: 50 }])) };
  assert.deepEqual(card.confirmContent('en', request, empties, config, 'en').drives.map((d) => [d.letter, d.checked]), [['d', true], ['e', false]]);

  // Two drives with a library and one without: libraries first, Steam's default checked and starred
  const two = { pcOn: true, free: card.freeSpace(freeState([C, F, D])) };
  const c = card.confirmContent('en', request, two, config, 'en');
  assert.equal(c.driveLabel, 'Install to');
  assert.deepEqual(c.drives, [
    { letter: 'c', label: 'C:', free: '360\u00a0GB free', note: '', usb: false, media: '', fits: null, fit: '', default: false, checked: false },
    { letter: 'f', label: 'F:', free: '1,179\u00a0GB free', note: '', usb: false, media: '', fits: null, fit: '', default: true, checked: true },
    { letter: 'd', label: 'D:', free: '1,595\u00a0GB free', note: 'new Steam library', usb: false, media: '', fits: null, fit: '', default: false, checked: false },
  ]);
  assert.equal(card.confirmContent('de', request, two, config, 'de').driveLabel, 'Installieren auf');
  // Without a marked default the first drive is checked
  const unmarked = { pcOn: true, free: card.freeSpace(freeState([C, { ...F, default: false }])) };
  assert.deepEqual(card.confirmContent('en', request, unmarked, config, 'en').drives.map((d) => d.checked), [true, false]);
  // An entry without a drive letter cannot be sent, a letter is offered once (a library wins over
  // a drive without one), a default mark on a drive without a library counts for nothing
  const odd = { pcOn: true, free: card.freeSpace(freeState([F, { ...C, drive: '' }, { ...F, default: false, library: 'F:\\Other' }, { ...D, drive: 'F:' }, { ...D, drive: '' }])) };
  assert.deepEqual(card.driveChoices(odd), []);
  assert.deepEqual(card.freeSpace(freeState([{ ...D, default: true }])).noLibrary.map((d) => d.default), [false]);
  // A USB drive is marked as one (library or not), the badge shows in the dialog
  const usb = { pcOn: true, free: card.freeSpace(freeState([F, { ...D, drive: 'G:', bus: 'USB' }])) };
  const u = card.confirmContent('en', request, usb, config, 'en');
  assert.deepEqual(u.drives.map((d) => [d.letter, d.usb]), [['f', false], ['g', true]]);
  const uHtml = card.RENDERERS.tiles.dialog({ lang: 'en' }, u);
  assert.match(uHtml, /G:<span class="usb" title="USB drive">USB<\/span>/);
  assert.doesNotMatch(uHtml, /F:<span class="usb"/);
  assert.match(card.RENDERERS.tiles.dialog({ lang: 'de' }, card.confirmContent('de', request, usb, config, 'de')), /title="USB-Laufwerk"/);
  // Uninstall and the PC dialogs never offer drives
  assert.deepEqual(card.confirmContent('en', { action: 'uninstall', appid: '440', name: 'TF2' }, two, config, 'en').drives, []);
  assert.deepEqual(card.confirmContent('en', { action: 'pc_restart' }, two, config, 'en').drives, []);
});

test('the space check of the card counts the reserve as install.ps1 does', () => {
  // install.ps1: needed = size + max(5 GB, 10 % of the size); it stops when free < needed
  assert.deepEqual([card.SPACE_RESERVE_SHARE, card.SPACE_RESERVE_MIN_GB], [0.1, 5]);
  assert.equal(card.neededGb(10 * 1024), 15); // 10 GB: the minimum of 5 GB
  assert.equal(card.neededGb(100 * 1024), 110); // 100 GB: 10 %
  assert.equal(card.neededGb(50 * 1024), 55); // the two meet at 50 GB
  for (const size of [0, -1, null, undefined, 'x']) assert.equal(card.neededGb(size), 0);
  // Exactly the space needed fits, a bit less does not
  assert.deepEqual(card.driveFit(100 * 1024, 110), { fits: true, needed: 110, missing: 0 });
  assert.deepEqual(card.driveFit(100 * 1024, 79), { fits: false, needed: 110, missing: 31 });
  // Unknown size or free space: no check
  assert.equal(card.driveFit(0, 500), null);
  assert.equal(card.driveFit(1024, NaN), null);
  assert.equal(card.driveFit(1024, undefined), null);
});

test('the install dialog says per drive whether the game fits, with its media', () => {
  const config = card.normalizeConfig({ show_images: false });
  // F: the star, an HDD with 42 GB free; C: an SSD; E: an HDD without a library; G: a USB SSD without one
  const drives = [
    { ...F, free_gb: 42, media: 'hdd', bus: 'sata' },
    { ...C, media: 'ssd', bus: 'nvme' },
    { ...D, drive: 'E:', free_gb: 812, media: 'hdd', bus: 'sata' },
    { ...D, drive: 'G:', free_gb: 230, media: 'ssd', bus: 'usb' },
  ];
  const m = { pcOn: true, free: card.freeSpace(freeState(drives)) };
  const request = { action: 'install', appid: '1091500', name: 'Cyberpunk 2077', size: 66 * 1024 }; // needs 72.6 GB
  const c = card.confirmContent('en', request, m, config, 'en');
  assert.deepEqual(c.drives.map((d) => [d.letter, d.media, d.fits, d.fit, d.checked]), [
    ['f', 'HDD', false, 'does not fit (31 GB missing)', false],
    ['c', 'SSD', true, 'fits', true],
    ['e', 'HDD', true, 'fits', false],
    ['g', 'SSD', true, 'fits', false],
  ]);
  // The star is too full: the first drive it fits on is preselected, with the hint; no warning
  assert.equal(c.driveHint, 'F: is too full, C: preselected.');
  assert.deepEqual(c.warnings, []);
  const html = card.RENDERERS.tiles.dialog({ lang: 'en' }, c);
  assert.match(html, /F:<span class="dm"> · HDD<\/span>/);
  assert.match(html, /G:<span class="dm"> · SSD<\/span><span class="usb"/);
  assert.match(html, /<span class="df nofit">42 GB free · does not fit \(31 GB missing\)<\/span>/);
  assert.match(html, /<span class="df">360 GB free · fits<\/span>/);
  assert.match(html, /<p class="dhint" data-hint>F: is too full, C: preselected\.<\/p>/);
  assert.match(html, /<div data-warnings><\/div>/);
  // German
  const de = card.confirmContent('de', request, m, config, 'de');
  assert.deepEqual(de.drives.map((d) => d.fit), ['passt nicht (fehlen 31 GB)', 'passt', 'passt', 'passt']);
  assert.equal(de.driveHint, 'F: ist zu voll, C: vorgewählt.');

  // The star drive picked anyway: the warning, no hint; installing stays possible
  const picked = card.confirmContent('en', { ...request, drive: 'f' }, m, config, 'en');
  assert.deepEqual(picked.drives.map((d) => d.checked), [true, false, false, false]);
  assert.equal(picked.driveHint, '');
  assert.deepEqual(picked.warnings, ['Probably not enough space on F: (31 GB missing: needs 73 GB, 42 GB free).']);
  assert.match(card.RENDERERS.tiles.dialog({ lang: 'en' }, picked), /<div data-warnings><div id="dialog-warning" class="dwarn">/);
  assert.deepEqual(card.confirmContent('de', { ...request, drive: 'f' }, m, config, 'de').warnings,
    ['Auf F: ist voraussichtlich nicht genug Platz (fehlen 31 GB: braucht 73 GB, frei 42 GB).']);
  // The preselected drive picked again: the hint is back; a letter not offered counts for nothing
  assert.equal(card.confirmContent('en', { ...request, drive: 'c' }, m, config, 'en').driveHint, 'F: is too full, C: preselected.');
  assert.equal(card.confirmContent('en', { ...request, drive: 'z' }, m, config, 'en').drives.find((d) => d.checked).letter, 'c');
  // Another fitting drive picked: no hint, no warning
  const other = card.confirmContent('en', { ...request, drive: 'e' }, m, config, 'en');
  assert.deepEqual([other.driveHint, other.warnings], ['', []]);

  // It fits on the star: the star stays checked, no hint
  const small = card.confirmContent('en', { ...request, size: 10 * 1024 }, m, config, 'en');
  assert.deepEqual([small.drives.find((d) => d.checked).letter, small.driveHint, small.warnings], ['f', '', []]);
  assert.deepEqual(small.drives.map((d) => d.fit), ['fits', 'fits', 'fits', 'fits']);
  // Size unknown: no fit info, the media still shown, the star checked
  const unknown = card.confirmContent('en', { ...request, size: 0 }, m, config, 'en');
  assert.deepEqual(unknown.drives.map((d) => [d.media, d.fits, d.fit, d.checked]),
    [['HDD', null, '', true], ['SSD', null, '', false], ['HDD', null, '', false], ['SSD', null, '', false]]);
  assert.deepEqual([unknown.driveHint, unknown.warnings], ['', []]);
  const unknownHtml = card.RENDERERS.tiles.dialog({ lang: 'en' }, unknown);
  assert.match(unknownHtml, /<span class="df">42 GB free<\/span>/);
  assert.match(unknownHtml, /<p class="dhint" data-hint hidden><\/p>/);
  // Fits nowhere: the star stays checked, with the warning, no hint
  const huge = card.confirmContent('en', { ...request, size: 2000 * 1024 }, m, config, 'en');
  assert.deepEqual([huge.drives.find((d) => d.checked).letter, huge.driveHint], ['f', '']);
  assert.deepEqual(huge.drives.map((d) => d.fits), [false, false, false, false]);
  assert.equal(huge.warnings.length, 1);
  // Media unknown: no media text; size unknown: the free space alone
  assert.equal(card.driveMediaText({ media: '' }), '');
  assert.equal(card.driveFreeText({ free: '5 GB free', fit: '' }), '5 GB free');
  assert.equal(card.driveFreeClass({ fits: null }), 'df');
});

test('the preselection prefers drives with a library, then an SSD', () => {
  const choices = [
    { letter: 'e', newLibrary: true, media: 'ssd', free_gb: 900 },
    { letter: 'd', newLibrary: false, media: 'hdd', free_gb: 900 },
    { letter: 'c', newLibrary: false, media: 'ssd', free_gb: 900 },
    { letter: 'b', newLibrary: false, media: 'ssd', free_gb: 1 },
    { letter: 'h', newLibrary: true, media: 'hdd', free_gb: 900 },
  ];
  assert.equal(card.firstFittingDrive(choices, 1024).letter, 'c');
  assert.equal(card.firstFittingDrive(choices.filter((d) => d.letter !== 'c'), 1024).letter, 'd');
  assert.equal(card.firstFittingDrive(choices.filter((d) => !'cd'.includes(d.letter)), 1024).letter, 'e');
  // Unknown media counts as not an SSD, otherwise the PC's order
  assert.equal(card.firstFittingDrive([{ letter: 'x', media: '', free_gb: 900 }, { letter: 'y', media: 'hdd', free_gb: 900 }], 1024).letter, 'x');
  assert.equal(card.firstFittingDrive(choices, 0), null);
  assert.equal(card.firstFittingDrive(choices, 5000 * 1024), null);
  assert.equal(card.firstFittingDrive(undefined, 1024), null);
  // A drive with a library wins over an SSD without one
  const config = card.normalizeConfig({ show_images: false });
  const m = { pcOn: true, free: card.freeSpace(freeState([{ ...F, free_gb: 10 }, { ...D, drive: 'G:', media: 'ssd' }, { ...C, media: 'hdd' }])) };
  const c = card.confirmContent('en', { action: 'install', appid: '1', name: 'X', size: 30 * 1024 }, m, config, 'en');
  assert.equal(c.drives.find((d) => d.checked).letter, 'c');
  assert.equal(c.driveHint, 'F: is too full, C: preselected.');
});

test('the install dialog warns also with a single drive, which offers no choice', () => {
  const config = card.normalizeConfig({ show_images: false });
  const request = { action: 'install', appid: '1', name: 'X', size: 66 * 1024 };
  const one = { pcOn: true, free: card.freeSpace(freeState([{ ...F, free_gb: 42 }])) };
  const c = card.confirmContent('en', request, one, config, 'en');
  assert.deepEqual([c.drives, c.driveHint], [[], '']);
  assert.deepEqual(c.warnings, ['Probably not enough space on F: (31 GB missing: needs 73 GB, 42 GB free).']);
  // Just short of the limit (needs 110.44 GB, 110 free): the numbers never look equal
  const tight = { pcOn: true, free: card.freeSpace(freeState([{ ...F, free_gb: 110 }])) };
  assert.deepEqual(card.confirmContent('en', { ...request, size: 102810 }, tight, config, 'en').warnings,
    ['Probably not enough space on F: (1 GB missing: needs 111 GB, 110 GB free).']);
  // The only library without a star mark counts as the target too
  const unmarked = { pcOn: true, free: card.freeSpace(freeState([{ ...F, free_gb: 42, default: false }])) };
  assert.equal(card.confirmContent('en', request, unmarked, config, 'en').warnings.length, 1);
  assert.equal(card.installDrive(unmarked).label, 'F:');
  // Fits, size unknown, no data: no warning
  assert.deepEqual(card.confirmContent('en', { ...request, size: 10 * 1024 }, one, config, 'en').warnings, []);
  assert.deepEqual(card.confirmContent('en', { ...request, size: 0 }, one, config, 'en').warnings, []);
  assert.deepEqual(card.confirmContent('en', request, { pcOn: true }, config, 'en').warnings, []);
  assert.equal(card.installDrive({ pcOn: true }), null);
  // Uninstall never warns about space
  assert.deepEqual(card.confirmContent('en', { action: 'uninstall', appid: '1', name: 'X', size: 66 * 1024 }, one, config, 'en').warnings, []);
});

test('the install from the dialog sends the drive picked, nothing without a choice', () => {
  assert.deepEqual(card.installData('440', 'f'), { appid: '440', drive: 'f' });
  assert.deepEqual(card.installData('440', ''), { appid: '440' });
});

test('the tab Space lists all drives, the default starred, those without a library muted', () => {
  const free = card.freeSpace(freeState([F, C, D]));
  assert.deepEqual(card.driveRows('en', 'en', free), [
    { label: 'F:', path: 'F:\\SteamLibrary', text: '1,179\u00a0GB free of 1,863\u00a0GB', hasTotal: true, used: 37, default: true, library: true, note: '', usb: false, icon: 'mdi:harddisk', letter: 'f', games: null },
    { label: 'C:', path: 'C:\\Program Files (x86)\\Steam', text: '360\u00a0GB free of 476\u00a0GB', hasTotal: true, used: 24, default: false, library: true, note: '', usb: false, icon: 'mdi:harddisk', letter: 'c', games: null },
    { label: 'D:', path: '', text: '1,595\u00a0GB free of 1,863\u00a0GB', hasTotal: true, used: 14, default: false, library: false, note: 'no Steam library', usb: false, icon: 'mdi:harddisk', letter: 'd', games: null },
  ]);
  assert.deepEqual(card.driveRows('en', 'en', null), []);
  assert.deepEqual(card.driveRows('de', 'de', free).map((r) => r.note), ['', '', 'keine Steam-Bibliothek']);
  // Drives without a library are listed also when they are the only ones
  const onlyEmpty = card.freeSpace(freeState([D]));
  assert.deepEqual([onlyEmpty.drives, card.driveRows('en', 'en', onlyEmpty).map((r) => [r.label, r.library])], [[], [['D:', false]]]);
  assert.equal(card.t('de', 'drive_default'), 'Steam-Standard für neue Spiele');
});

test('the tab Space marks USB drives and picks the icon by the media', () => {
  assert.equal(card.driveIcon('ssd'), 'se:ssd');
  assert.equal(card.driveIcon('hdd'), 'mdi:harddisk');
  assert.equal(card.driveIcon(''), 'mdi:harddisk');
  assert.equal(card.driveIcon(undefined), 'mdi:harddisk');
  // As this PC: C: and E: SSD, F: and D: HDD, G: an empty USB disk; an old agent sends neither field
  const free = card.freeSpace(freeState([
    { ...F, bus: 'sata', media: 'hdd' },
    { ...C, bus: 'nvme', media: 'ssd' },
    { ...D, bus: 'sata', media: 'hdd' },
    { ...D, drive: 'E:', bus: 'raid', media: 'SSD' },
    { ...D, drive: 'G:', bus: 'usb', media: null },
    { ...D, drive: 'H:', bus: 42, media: 'flash' },
  ]));
  const rows = card.driveRows('en', 'en', free);
  assert.deepEqual(rows.map((r) => [r.label, r.library, r.usb, r.icon]), [
    ['F:', true, false, 'mdi:harddisk'],
    ['C:', true, false, 'se:ssd'],
    ['D:', false, false, 'mdi:harddisk'],
    ['E:', false, false, 'se:ssd'],
    ['G:', false, true, 'mdi:harddisk'],
    ['H:', false, false, 'mdi:harddisk'],
  ]);
  assert.deepEqual(free.noLibrary.map((d) => [d.bus, d.media]), [['sata', 'hdd'], ['raid', 'ssd'], ['usb', ''], ['', '']]);
  assert.deepEqual(card.freeSpace(freeState([F])).drives.map((d) => [d.bus, d.media]), [['', '']]);
});

test('the tab Space renders the drives without a library muted with their hint', () => {
  const config = card.normalizeConfig({});
  const m = { pcOn: true, free: card.freeSpace(freeState([{ ...F, media: 'hdd' }, { ...D, drive: 'G:', bus: 'usb' }, { ...D, drive: 'E:', media: 'ssd' }])) };
  const html = card.RENDERERS.tiles.free({ lang: 'en', locale: 'en', m, config });
  const tiles = html.split('<div class="tile drive').slice(1);
  assert.equal(tiles.length, 3);
  assert.doesNotMatch(tiles[0], /nolib/);
  assert.match(tiles[0], /mdi:harddisk/);
  assert.match(tiles[0], /class="star"/);
  assert.match(tiles[0], /% used/);
  assert.match(tiles[1], /^ nolib"/);
  assert.match(tiles[1], /G:<span class="usb" title="USB drive">USB<\/span>/);
  assert.match(tiles[1], /1,595\u00a0GB free of 1,863\u00a0GB · no Steam library/);
  assert.doesNotMatch(tiles[1], /% used|class="bar|class="star"/);
  assert.match(tiles[2], /^ nolib"/);
  assert.match(tiles[2], /<svg class="se-icon"[^>]*data-icon="se:ssd"><path fill-rule="evenodd" d="M6,2H18/);
  assert.doesNotMatch(tiles[2], /class="usb"/);
  // Only drives without a library (an old agent's state): the most free space first, then those
  const empty = { pcOn: true, free: card.freeSpace(freeState([D])) };
  const emptyHtml = card.RENDERERS.tiles.free({ lang: 'de', locale: 'de', m: empty, config });
  assert.equal(emptyHtml.split('<div class="tile drive').length - 1, 2);
  assert.match(emptyHtml, /keine Steam-Bibliothek/);
});

test('an installed game names its drive with its size, nothing more from an older PC', () => {
  assert.equal(card.driveOf('F:'), 'F:');
  assert.equal(card.driveOf(' e '), 'E:');
  assert.equal(card.driveOf('F:\SteamLibrary'), '');
  assert.equal(card.driveOf(null), '');
  assert.equal(card.driveOf(5), '');
  const games = card.libraryGames(state('2', { games: [
    { appid: '1', name: 'A', size_mb: 31744, drive: 'f:' },
    { appid: '2', name: 'B', size_mb: 512 },
  ] }));
  assert.deepEqual(games.map((g) => g.drive), ['F:', '']);
  assert.equal(card.sizeOnDrive('en', 'en', games[0]), '31\u00a0GB on F:');
  assert.equal(card.sizeOnDrive('de', 'de', games[0]), '31\u00a0GB auf F:');
  assert.equal(card.sizeOnDrive('en', 'en', games[1]), card.formatSize(512, 'en'));
  assert.equal(card.sizeOnDrive('de', 'de', { size_mb: 0, drive: 'C:' }), 'auf C:');
  assert.equal(card.sizeOnDrive('en', 'en', null), '');
  // The sheet and the rows of the tiles style say it, the capsule only in its accessible name
  const m = libModel();
  m.library[3] = { ...m.library[3], drive: 'F:' };
  assert.deepEqual(card.sheetMeta('en', 'en', card.gameDetail('en', '4', m)),
    ['4\u00a0GB on F:', `last played ${card.formatDate('2026-10-01', 'en')}`, 'update 2\u00a0GB']);
  const tile = card.gridTile('en', 'en', 'installed', m.library[3], m);
  assert.equal(tile.meta[0], '4\u00a0GB');
  assert.match(tile.label, /, 4\u00a0GB on F:, last played /);
  const config = card.normalizeConfig({});
  const view = {
    lang: 'de', locale: 'de', m: { ...m, playing: '', pcOn: true }, config,
    busy: () => false, tab: 'installed',
  };
  const html = card.RENDERERS.tiles.installed(view);
  assert.match(html, /4\u00a0GB auf F:/);
  assert.doesNotMatch(html, /512\u00a0MB auf/);
});

test('the games per drive: largest first, null from an older PC', () => {
  const library = [
    { appid: '1', name: 'Small', size_mb: 100, drive: 'F:' },
    { appid: '2', name: 'Big', size_mb: 90000, drive: 'F:' },
    { appid: '3', name: 'Other', size_mb: 5000, drive: 'C:' },
    { appid: '4', name: 'Same b', size_mb: 100, drive: 'F:' },
    { appid: '5', name: 'Nowhere', size_mb: 700, drive: '' },
  ];
  const map = card.gamesByDrive(library);
  assert.deepEqual([...map.keys()], ['f', 'c']);
  assert.deepEqual(map.get('f').map((g) => g.appid), ['2', '4', '1']);
  assert.equal(card.gamesByDrive(library.map((g) => ({ ...g, drive: '' }))), null);
  assert.equal(card.gamesByDrive(null), null);
  const free = card.freeSpace(freeState([F, C, { ...D, drive: 'E:', library: 'E:\\SteamLibrary' }, D]));
  const rows = card.driveRows('en', 'en', free, card.libraryGames(state('5', { games: library })));
  assert.deepEqual(rows.map((r) => [r.label, r.games && r.games.map((g) => g.appid)]),
    [['F:', ['2', '4', '1']], ['C:', ['3']], ['E:', []], ['D:', null]]);
  // An older PC (no drive per game) or an unknown library: no lists
  assert.deepEqual(card.driveRows('en', 'en', free, card.libraryGames(state('1', { games: [{ appid: '1', name: 'A' }] }))).map((r) => r.games),
    [null, null, null, null]);
  assert.deepEqual(card.driveRows('en', 'en', free, null).map((r) => r.games), [null, null, null, null]);
});

test('the tab Space opens a drive to list its games, a button with aria-expanded', () => {
  const config = card.normalizeConfig({});
  const library = card.libraryGames(state('3', { games: [
    { appid: '1', name: 'Small <b>', size_mb: 100, drive: 'F:' },
    { appid: '2', name: 'Big', size_mb: 90000, drive: 'F:' },
    { appid: '3', name: '', size_mb: 5000, drive: 'C:' },
  ] }));
  const m = { pcOn: true, library, names: new Map(), free: card.freeSpace(freeState([F, C, { ...D, drive: 'E:', library: 'E:\\SteamLibrary' }, D])) };
  const render = (openDrive, lang = 'en') => card.RENDERERS.tiles.free({ lang, locale: lang, m, config, openDrive });
  const closed = render('');
  const tiles = closed.split('<div class="tile drive').slice(1);
  assert.equal(tiles.length, 4);
  assert.match(tiles[0], /^ exp"/);
  assert.match(tiles[0], /<button type="button" class="dhead" data-action="drive" data-drive="f" data-key="drive:f"\s+aria-expanded="false" aria-controls="drive-games-f">/);
  assert.match(tiles[0], /1,179\u00a0GB free of 1,863\u00a0GB · 2 games/);
  assert.match(tiles[1], /· 1 game</);
  assert.match(tiles[2], /· 0 games/);
  assert.doesNotMatch(tiles[3], /dhead/);
  assert.doesNotMatch(closed, /class="dgames"/);
  const open = render('f').split('<div class="tile drive').slice(1);
  assert.match(open[0], /^ exp open"/);
  assert.match(open[0], /aria-expanded="true"/);
  assert.match(open[0], /<ul class="dgames" id="drive-games-f"><li><span class="gn" title="Big">Big<\/span>\s*<span class="gs">87[.,]9\u00a0GB<\/span><\/li><li><span class="gn" title="Small &lt;b&gt;">/);
  assert.doesNotMatch(open[1], /dgames/);
  // A game without a name gets the fallback, a library without games says so
  assert.ok(render('c').includes(`title="${card.t('en', 'game_fallback', { appid: '3' })}"`));
  assert.match(render('e', 'de'), /<div class="dgames" id="drive-games-e"><p class="hint">Keine Spiele auf diesem Laufwerk\.<\/p><\/div>/);
  assert.match(render('', 'de'), /· 2 Spiele/);
  // An older PC: the drives stay plain tiles
  const old = { ...m, library: card.libraryGames(state('1', { games: [{ appid: '1', name: 'A', size_mb: 1 }] })) };
  const oldHtml = card.RENDERERS.tiles.free({ lang: 'en', locale: 'en', m: old, config, openDrive: 'f' });
  assert.doesNotMatch(oldHtml, /dhead|dgames|\d games/);
});

// --- moving a game to another drive ---------------------------------------------------------

const OLD_F = { appid: '20', name: 'Old F', size_mb: 60 * 1024, workshop_mb: 2048, last_played: '', unavailable: false, drive: 'F:' };
// F: (the game's drive) and C: with a library, E: (HDD) and G: (USB SSD) without one
const moveDrives = () => card.freeSpace(freeState([
  { ...F, free_gb: 42, media: 'ssd', bus: 'nvme' },
  { ...C, free_gb: 50, media: 'ssd', bus: 'nvme' },
  { ...D, drive: 'E:', free_gb: 812, media: 'hdd', bus: 'sata' },
  { ...D, drive: 'G:', free_gb: 230, media: 'ssd', bus: 'usb' },
]));
const moveModel = (extra = {}) => ({
  pcOn: true, playing: '', job: { active: false }, open: new Set(), updates: [], autoUpdate: new Set(),
  library: [OLD_F], notInstalled: { known: true, games: [] }, names: new Map(), free: moveDrives(), move: null, ...extra,
});

test('the move and its last end come from the download progress sensor', () => {
  const progress = state('0', {
    job: [], queue: [],
    move: { appid: 20, name: null, from: 'f:', drive: 'E:', phase: 'copy', percent: 41.6, copied_gb: 25, total_gb: 62 },
    move_result: { appid: '10', name: 'Ten', from: 'E:', drive: 'F:', result: 'error', reason_code: 'no_space', warning: null, updated: '2026-11-11T18:05:00+00:00' },
  });
  assert.deepEqual(card.moveInfo(progress), { appid: '20', name: '', from: 'F:', drive: 'E:', phase: 'copy', percent: 42 });
  assert.deepEqual(card.moveResultOf(progress), {
    appid: '10', name: 'Ten', from: 'E:', drive: 'F:', ok: false, reason: 'no_space', warning: '', updated: '2026-11-11T18:05:00+00:00',
  });
  assert.equal(card.moveInfo(state('0', { move: null })), null);
  assert.equal(card.moveInfo(undefined), null);
  assert.equal(card.moveResultOf(state('0', {})), null);
  assert.equal(card.moveMark(null), '');
  assert.equal(card.moveMark(card.moveResultOf(progress)), '10|false|no_space|2026-11-11T18:05:00+00:00');
  // The workshop content is kept per game (0 without)
  const library = card.libraryGames(state('1', { games: [{ appid: '20', size_mb: 100, workshop_mb: 30 }, { appid: '21' }] }));
  assert.deepEqual(library.map((g) => g.workshop_mb), [30, 0]);

  // The model: a move counts as a job (the PC section), not as Steam downloading by itself
  const ids = { entities: { download_progress: 'sensor.p', steam_busy: 'binary_sensor.busy' }, agent: {} };
  const m = card.buildModel({ 'sensor.p': progress, 'binary_sensor.busy': state('on') }, ids);
  assert.equal(m.move.appid, '20');
  assert.equal(m.moveResult.reason, 'no_space');
  assert.deepEqual([m.jobRunning, m.steamDownloading], [true, false]);
  assert.equal(card.pcHold('en', { ...m, pcOn: true }), card.t('en', 'hold', { reasons: 'job running' }));
  const chips = card.statusChips('en', { ...m, updates: [] }, 'en').filter((c) => c.key === 'move' || c.key === 'download');
  assert.deepEqual(chips.map((c) => [c.key, c.icon, c.text]), [['move', 'mdi:folder-move-outline', 'Moving 42 %']]);
  assert.deepEqual(card.moveDetails('de', m.move), ['F: → E:', 'kopieren']);
  assert.deepEqual(card.moveDetails('en', { from: '', drive: 'E:', phase: 'requested' }), ['waiting for the PC']);
});

test('the sheet offers Move where Uninstall is, when the PC reports another drive', () => {
  const config = card.normalizeConfig({ style: 'library' });
  const actions = (m, appid = '20') => card.sheetActions(card.gameDetail('en', appid, m), m, config);
  assert.deepEqual(actions(moveModel()), ['launch', 'auto', 'move', 'uninstall']);
  // The drives it may go to: all others, also one alone; none without the game's drive
  assert.deepEqual(card.moveChoices(moveModel(), OLD_F).map((d) => [d.letter, d.newLibrary]), [['c', false], ['e', true], ['g', true]]);
  const two = moveModel({ free: card.freeSpace(freeState([{ ...F }, { ...D, drive: 'E:' }])) });
  assert.deepEqual(card.moveChoices(two, OLD_F).map((d) => d.letter), ['e']);
  assert.deepEqual(actions(two), ['launch', 'auto', 'move', 'uninstall']);
  const alone = moveModel({ free: card.freeSpace(freeState([{ ...F }])) });
  assert.deepEqual(actions(alone), ['launch', 'auto', 'uninstall']);
  const older = moveModel({ library: [{ ...OLD_F, drive: '' }] });
  assert.deepEqual(actions(older), ['launch', 'auto', 'uninstall']);
  assert.deepEqual(card.moveChoices(moveModel(), { ...OLD_F, unavailable: true }), []);
  assert.deepEqual(actions(moveModel({ free: null })), ['launch', 'auto', 'uninstall']);
  // Not while a Steam job runs, nor for the running game
  assert.deepEqual(actions(moveModel({ job: { active: true, appid: '9' } })), ['launch', 'auto', 'uninstall']);
  assert.deepEqual(actions(moveModel({ playing: '20' })), ['auto']);
  // Nor while another game runs or the queue waits (HA refuses then)
  assert.deepEqual(actions(moveModel({ gameRunning: true, playing: '9' })), ['launch', 'auto', 'uninstall']);
  assert.deepEqual(actions(moveModel({ job: { active: false, queue: ['9'] } })), ['launch', 'auto', 'uninstall']);
  // The tiles style: no start while a move runs, the game being moved has no Uninstall
  const row = (appid, move) => card.rowActions({ list: 'installed', game: { appid }, pcOn: true, open: new Set(), move });
  assert.deepEqual([row('20', { appid: '21' }).launch, row('20', { appid: '21' }).uninstall], [false, true]);
  assert.deepEqual([row('21', { appid: '21' }).launch, row('21', { appid: '21' }).uninstall], [false, false]);
  assert.deepEqual([row('20', null).launch, row('20', null).uninstall], [true, true]);
  // While a move runs: no other move and no game start; the game being moved only has its switch
  const moving = moveModel({ library: [OLD_F, { ...OLD_F, appid: '21', name: 'Other' }], move: { appid: '21', phase: 'copy', percent: 5 } });
  assert.deepEqual(actions(moving), ['auto', 'uninstall']);
  assert.deepEqual(actions(moving, '21'), ['auto']);
  const detail = card.gameDetail('en', '21', moving);
  assert.equal(detail.moving, true);
  assert.deepEqual(card.sheetFlags('en', detail).map((f) => f.key), ['tag_moving']);
  assert.deepEqual(card.rowState('installed', moving.library[1], moving).tags.map((x) => x.key), ['tag_moving']);
  const tile = card.gridTile('en', 'en', 'installed', moving.library[1], moving);
  assert.deepEqual([tile.flags.map((f) => f.key), JSON.stringify(tile).includes('5 %')], [['tag_moving'], false]);
  // The button in the sheet, before Uninstall
  const view = { lang: 'en', locale: 'en', config, m: moveModel(), busy: () => false, failed: () => false };
  const html = card.RENDERERS.library.sheet(view, card.gameDetail('en', '20', view.m), actions(view.m));
  assert.match(html, /data-action="move" data-appid="20"[\s\S]*mdi:folder-move-outline[\s\S]*<span>Move<\/span>/);
  assert.ok(html.indexOf('data-action="move"') < html.indexOf('data-action="uninstall"'));
});

test('the move dialog: the other drives with their fit, the first fitting one preselected', () => {
  const config = card.normalizeConfig({ show_images: false });
  const request = { action: 'move', appid: '20', name: 'Old F' };
  const m = moveModel();
  // 60 GB + 2 GB workshop content + the reserve of 6 GB: 68 GB
  assert.equal(card.neededGb(60 * 1024, 2048), 68);
  assert.equal(card.neededGb(60 * 1024), 66);
  const c = card.confirmContent('en', request, m, config, 'en');
  assert.equal(c.title, 'Move Old F?');
  assert.equal(c.text, `The game (${card.formatSize(62 * 1024, 'en')}) is copied from F: to the drive picked; Steam closes for a moment near the end. Nothing is downloaded again, the old folder is deleted at the end.`);
  assert.deepEqual([c.ok, c.okIcon, c.tone, c.driveLabel, c.driveHint], ['Move', 'mdi:folder-move-outline', 'blue', 'Move to', '']);
  // The game's own drive is not offered; C: (library, SSD) is too small, so the SSD without a
  // library before the HDD without one
  assert.deepEqual(c.drives.map((d) => [d.letter, d.media, d.fits, d.fit, d.note, d.checked]), [
    ['c', 'SSD', false, 'does not fit (18 GB missing)', '', false],
    ['e', 'HDD', true, 'fits', 'new Steam library', false],
    ['g', 'SSD', true, 'fits', 'new Steam library', true],
  ]);
  assert.deepEqual(c.warnings, []);
  const html = card.RENDERERS.tiles.dialog({ lang: 'en' }, c);
  assert.match(html, /<legend>Move to<\/legend>/);
  assert.match(html, /G:<span class="dm"> · SSD<\/span><span class="usb"/);
  assert.match(html, /<span class="df nofit">50 GB free · does not fit \(18 GB missing\)<\/span>/);
  // A drive picked: kept, with the warning when it does not fit
  const picked = card.confirmContent('en', { ...request, drive: 'c' }, m, config, 'en');
  assert.equal(picked.drives.find((d) => d.checked).letter, 'c');
  assert.deepEqual(picked.warnings, ['Probably not enough space on C: (18 GB missing: needs 68 GB, 50 GB free).']);
  // A library drive it fits on comes first
  const roomy = moveModel({ free: card.freeSpace(freeState([{ ...F, media: 'ssd' }, { ...C, free_gb: 300, media: 'hdd' }, { ...D, drive: 'G:', media: 'ssd' }])) });
  assert.equal(card.confirmContent('en', request, roomy, config, 'en').drives.find((d) => d.checked).letter, 'c');
  // Fits nowhere: the first drive, with the warning; German; PC off
  const full = moveModel({ free: card.freeSpace(freeState([{ ...F }, { ...C, free_gb: 10 }])) });
  const de = card.confirmContent('de', request, { ...full, pcOn: false }, config, 'de');
  assert.equal(de.drives.find((d) => d.checked).letter, 'c');
  assert.deepEqual(de.warnings, ['Auf C: ist voraussichtlich nicht genug Platz (fehlen 58 GB: braucht 68 GB, frei 10 GB).']);
  assert.equal(de.title, 'Old F verschieben?');
  assert.ok(de.text.endsWith(card.t('de', 'confirm_pc_off')));
  assert.equal(de.driveLabel, 'Verschieben nach');
  // The game left the library meanwhile: no drives, the text without a size
  const gone = card.confirmContent('en', request, moveModel({ library: [] }), config, 'en');
  assert.deepEqual([gone.drives, gone.warnings], [[], []]);
  assert.equal(gone.text, card.t('en', 'confirm_move_text', { drive: '?' }));
});

test('a move: its answer, its end as a message and its row while it runs', () => {
  assert.deepEqual(card.responseMessage('en', 'move', 'Old F', { sent: true }),
    { type: 'success', text: 'Moving Old F started', track: true });
  assert.deepEqual(card.responseMessage('de', 'move', 'Old F', { sent: false, reason: 'same_drive' }),
    { type: 'error', text: 'Old F: nicht ausgeführt, das Spiel liegt schon auf diesem Laufwerk', track: false });
  assert.equal(card.responseMessage('en', 'launch', 'Raft', { sent: false, reason: 'moving' }).text, 'Raft: not done, a game is being moved');
  assert.equal(card.reasonText('en', 'job_running'), 'another Steam job is running');

  const end = (fields) => state('0', { move_result: { appid: '20', from: 'F:', drive: 'E:', updated: '2026-11-11T18:05:00+00:00', ...fields } });
  const before = card.moveMark(card.moveResultOf(end({ result: 'ok', updated: '2026-11-10T10:00:00+00:00' })));
  const tracked = { action: 'move', appid: '20', name: 'Old F', mark: before };
  assert.deepEqual(card.completionMessage('en', tracked, undefined, undefined, end({ result: 'ok' })),
    { type: 'success', text: 'Old F moved to E:' });
  assert.match(card.completionMessage('en', tracked, undefined, undefined, end({ result: 'ok', warning: 'source_not_deleted' })).text,
    /^Old F moved to E:; the old folder could not be deleted completely/);
  assert.deepEqual(card.completionMessage('de', tracked, undefined, undefined, end({ result: 'error', reason_code: 'copy_failed' })),
    { type: 'error', text: 'Old F: nicht verschoben, Kopieren fehlgeschlagen' });
  // game_running of a move: also a program from the game's folder (the code alone stays as it was)
  assert.equal(card.completionMessage('en', tracked, undefined, undefined, end({ result: 'error', reason_code: 'game_running' })).text,
    "Old F: not moved, a game or a program from the game's folder is running");
  assert.equal(card.reasonText('en', 'game_running'), 'a game is running');
  // The end seen when it was sent, another game's end, none: not done yet
  assert.equal(card.completionMessage('en', tracked, undefined, undefined, end({ result: 'ok', updated: '2026-11-10T10:00:00+00:00' })), null);
  assert.equal(card.completionMessage('en', tracked, undefined, undefined, end({ appid: '21', result: 'ok' })), null);
  assert.equal(card.completionMessage('en', tracked, undefined, undefined, undefined), null);
  // Seen running, then gone without an end (HA stopped waiting for the PC): a message
  const gone = end({ result: 'ok', updated: '2026-11-10T10:00:00+00:00' });
  assert.equal(card.completionMessage('en', { ...tracked, seen: true },
    undefined, undefined, state('0', { ...gone.attributes, move: { appid: '20', phase: 'copy' } })), null);
  assert.deepEqual(card.completionMessage('en', { ...tracked, seen: true }, undefined, undefined, gone),
    { type: 'error', text: 'Old F: not moved, no answer from the PC' });

  // While it runs: a row like a job in both styles, the percent in it, the route and the phase
  const m = {
    ...moveModel({ move: { appid: '20', name: 'Old F', from: 'F:', drive: 'E:', phase: 'sync', percent: 97 } }),
    job: card.jobInfo(undefined, undefined),
    images: {},
  };
  const view = { lang: 'en', locale: 'en', config: card.normalizeConfig({ show_images: false }), m, busy: () => false, failed: () => false };
  const tile = card.RENDERERS.tiles.move(view);
  assert.match(tile, /Moving Old F/);
  assert.match(tile, /F: → E: · Steam closed, copying the rest/);
  assert.match(tile, /<div class="big">97 %<\/div>/);
  assert.match(tile, /aria-valuenow="97"/);
  const row = card.RENDERERS.library.move(view);
  assert.match(row, /<span class="pct">97 %<\/span>/);
  assert.match(row, /data-k="d">F: → E: · Steam closed, copying the rest</);
  const parts = card.RENDERERS.library.parts({ ...view, config: card.normalizeConfig({ sections: ['status'] }), tab: '' });
  assert.ok(parts.some(([key]) => key === 'move'));
  // Its name before the PC sent it: from the names of the model
  const early = { ...m, move: { appid: '20', name: '', from: '', drive: 'E:', phase: 'requested', percent: 0 }, names: new Map([['20', 'Old F']]) };
  assert.match(card.RENDERERS.library.move({ ...view, m: early }), /Moving Old F[\s\S]*waiting for the PC/);
});

test('muted updates come last, are calm and do not count', () => {
  const updates = state('1', {
    games: [
      { appid: '1', name: 'Alpha', size_mb: 20 },
      { appid: '2', name: 'Zeta', size_mb: 10 },
      { appid: '3', name: 'Beta', size_mb: 5 },
    ],
    muted: ['1'],
  });
  const games = card.updateGames(updates);
  assert.deepEqual(games.map((g) => [g.appid, g.muted]), [['3', false], ['2', false], ['1', true]]);
  assert.deepEqual([...card.mutedSet(updates)], ['1']);
  const m = { ...tabModel(), updates: games };
  assert.deepEqual(card.countedUpdates(m).map((g) => g.appid), ['3', '2']);
  assert.equal(card.tabCount('updates', m), '2');
  assert.equal(card.tabCount('updates', { ...m, updates: [games[2]] }), '');
  // The tab stays while only muted updates are there: they can still be updated by hand
  assert.deepEqual(card.cardTabs(card.normalizeConfig({}), { ...m, updates: [games[2]] }).includes('updates'), true);
  const keys = (r) => r.tags.map((x) => x.key);
  const muted = card.rowState('updates', games[2], { ...rowModel, updates: games });
  assert.deepEqual([muted.tone, keys(muted)], ['neutral', ['badge_muted']]);
  const installed = card.rowState('installed', { appid: '1' }, { ...rowModel, playing: '', updates: games });
  assert.deepEqual([installed.tone, keys(installed)], ['neutral', []]);
  const chips = card.statusChips('en', chipModel({ updates: [games[2]] }), 'en');
  assert.deepEqual(chips.find((c) => c.key === 'updates').text, 'everything up to date');
  const detail = card.gameDetail('en', '1', { ...m, muted: card.mutedSet(updates) });
  assert.equal(detail.muted, true);
});

test('times and numbers follow the formats of the profile, not only its language', () => {
  const fmt = (locale) => card.formatLocaleOf({ locale });
  // 'language', 'system' and nothing set: the language as before
  assert.deepEqual(fmt({ language: 'de', number_format: 'system', time_format: 'system' }),
    { tag: 'de', number: 'de', grouping: true, hourCycle: undefined });
  assert.deepEqual(fmt({ language: 'en', number_format: 'language', time_format: 'language' }),
    { tag: 'en', number: 'en', grouping: true, hourCycle: undefined });
  assert.equal(card.formatLocaleOf(undefined).tag, 'en');
  // English with 24 hours, German with 12 hours
  assert.equal(card.formatClock('20:00', fmt({ language: 'en', time_format: '24' })), '20:00');
  assert.match(card.formatClock('20:00', fmt({ language: 'de', time_format: '12' })), /PM/);
  assert.match(card.formatClock('20:00', fmt({ language: 'en', time_format: 'language' })), /^8:00\sPM$/);
  assert.equal(card.formatSince('2026-11-03T20:00:00', fmt({ language: 'en', time_format: '24' }), new Date('2026-11-03T21:00:00')), '20:00');
  // Numbers: decimal_comma is 1.234,5, comma_decimal 1,234.5, none without grouping
  assert.equal(card.formatSize(3277, fmt({ language: 'en', number_format: 'decimal_comma' })), '3,2 GB');
  assert.equal(card.formatSize(3277, fmt({ language: 'de', number_format: 'comma_decimal' })), '3.2 GB');
  assert.equal(card.formatGb(1234.5, 'en'), '1,235 GB');
  assert.equal(card.formatGb(1234.5, fmt({ language: 'en', number_format: 'none' })), '1235 GB');
  assert.equal(card.formatRate(12345678, 'B/s', fmt({ language: 'en', number_format: 'decimal_comma' })), '12,3 MB/s');
  // A plain tag still works, and the text language stays the card's own choice
  assert.equal(card.formatSize(3277, 'de'), '3,2 GB');
  assert.equal(card.languageOf({ locale: { language: 'en', number_format: 'decimal_comma' } }), 'en');
});

test('the uninstall confirmation names the workshop content it deletes too', () => {
  const config = card.normalizeConfig({ show_images: false });
  const m = {
    pcOn: true,
    library: [{ appid: '20', size_mb: 2048, workshop_mb: 25 * 1024 }, { appid: '21', size_mb: 1024, workshop_mb: 0 }],
  };
  const request = { action: 'uninstall', appid: '20', name: 'Mods', size: 2048 };
  assert.equal(card.confirmContent('en', request, m, config, 'en').text,
    'The game files and workshop content (27 GB) will be deleted from the PC.');
  assert.equal(card.confirmContent('de', request, m, config, 'de').text,
    'Die Spieldateien und die Workshop-Inhalte (27 GB) werden vom PC gelöscht.');
  // Without workshop content, or not in the library: the game's size as before
  assert.equal(card.confirmContent('en', { ...request, appid: '21', size: 1024 }, m, config, 'en').text,
    'The game files (1 GB) will be deleted from the PC.');
  assert.equal(card.confirmContent('en', { ...request, appid: '99' }, m, config, 'en').text,
    'The game files (2 GB) will be deleted from the PC.');
  // An unknown size of the game: no size at all, not the workshop's alone
  assert.equal(card.confirmContent('en', { ...request, size: 0 }, m, config, 'en').text, card.t('en', 'confirm_uninstall_text_nosize'));
});

test('open PC details are updated in place: the buttons stay while the facts change', () => {
  const m = {
    pcOn: true, agentOnline: true, pcEntity: 'switch.pc', restartEntity: 'button.r', lockEntity: 'button.l',
    forceShutdownEntity: 'button.f', steamRunning: true, steamBusy: false, jobRunning: false, steamDownloading: false,
    lastInput: '', safe: true, offAt: '', pluginBusy: false, autoOffKnown: true, autoOffOn: true,
    network: { value: 1200, unit: 'B/s' },
  };
  const view = { lang: 'en', locale: 'en', m, details: true, busy: () => false, wakeBusy: false, failed: () => false };
  const later = { ...view, m: { ...m, network: { value: 5600, unit: 'B/s' } } };
  const feat = (html) => /<div class="feat" data-k="feat">[\s\S]*?<\/div>/.exec(html)[0];
  for (const style of ['tiles', 'library']) {
    const before = card.RENDERERS[style].pc(view);
    const after = card.RENDERERS[style].pc(later);
    assert.notEqual(before, after, style);
    // The section and the details carry data-morph, each fact its key, so morphElement keeps the rest
    assert.match(before, style === 'tiles' ? /^<section class="pc" data-morph>/ : /^<section class="lpc" data-morph /);
    assert.match(before, /id="pc-details" data-k="st" data-morph role="group"/);
    assert.match(before, /data-k="network"/);
    assert.match(before, /<div class="trow" data-k="hd">/);
    assert.match(before, /<div class="trow auto-off" data-k="ao">/);
    assert.match(before, /<div class="force" data-k="force">/);
    assert.equal(feat(after), feat(before), style);
    // Closed: the empty target keeps its key, so opening updates it in place too
    assert.match(card.RENDERERS[style].pc({ ...view, details: false }), /id="pc-details" data-k="st" data-morph hidden>/);
  }
  assert.match(card.RENDERERS.tiles.pc(view), /<div class="tile" data-k="tile" data-morph>/);
});

test('a core entity missing right after steam_engine/entities does not ask again and again', () => {
  const ids = { pc: 'switch.pc', updates: 'sensor.u', not_installed: 'sensor.n', download_progress: 'sensor.d', current_game: 'sensor.g' };
  const states = { 'switch.pc': {}, 'sensor.u': {}, 'sensor.n': {}, 'sensor.d': {}, 'sensor.g': {} };
  assert.deepEqual(card.missingEntities(ids, states), []);
  assert.equal(card.missingEntities(undefined, undefined).length, 5);
  // Disabled: named in the answer but not in hass.states, or left out of the answer
  const withoutGame = { ...states, 'sensor.g': undefined };
  assert.deepEqual(card.missingEntities(ids, withoutGame), ['current_game=sensor.g']);
  const idsWithout = { ...ids, current_game: undefined };
  assert.deepEqual(card.missingEntities(idsWithout, withoutGame), ['current_game=']);
  // Known after the answer: no reason to ask again, it stays known while it is missing
  const known = card.missingEntities(idsWithout, withoutGame);
  assert.deepEqual(card.splitMissing(known, known), { fresh: [], known: ['current_game='] });
  // Another entity renamed meanwhile: fresh, asked again
  const renamed = card.missingEntities(idsWithout, { ...withoutGame, 'sensor.u': undefined });
  assert.deepEqual(card.splitMissing(renamed, known), { fresh: ['updates=sensor.u'], known: ['current_game='] });
  // Back in hass.states: forgotten, so missing again later counts as fresh
  assert.deepEqual(card.splitMissing([], ['current_game=sensor.g']), { fresh: [], known: [] });
  assert.deepEqual(card.splitMissing(['current_game=sensor.g'], []), { fresh: ['current_game=sensor.g'], known: [] });
  // The images are subscribed again once nothing fresh is missing any more
  assert.equal(card.imagesResubscribeDue(true, card.splitMissing(known, known).fresh.length > 0, false), true);
});
