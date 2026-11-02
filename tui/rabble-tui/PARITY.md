# Parity with the Python UI (S296, Lane S)

Checklist source: `tests/test_ui_app.py`, `test_ui_transcript_scroll.py`, `test_ui_permission.py`, `test_ui_prefs.py`, `test_ui_theme.py`.
Rust tests live beside the code (`cargo test --offline --release` from `tui/`). `app::` = `src/app.rs` tests.

## test_ui_app.py
| Python test | Rust |
|---|---|
| test_text_rendered_once_and_header | `app::text_is_rendered_once_and_the_header_shows_the_model`, `transcript::text_is_drawn_once_when_the_block_follows_its_deltas` |
| test_tool_card_with_result | `app::a_tool_card_gets_its_result`, `transcript::tool_card_expands_to_the_input` |
| test_organ_turn_notices | `app::organ_turns_become_notices_and_background_turns_are_named` |
| test_escape_model_mode | `app::model_and_mode_commands_reach_the_engine_and_stick`, `model_picker_opens_without_an_argument_and_enter_chooses`, `interrupt_clears_pending_asks_and_the_placeholder` (Esc) |
| test_unknown_slash_passes_through | `app::an_unknown_slash_command_goes_to_the_engine_as_typed` |
| test_ctrl_c_twice_exits | `app::ctrl_c_twice_quits_once_is_a_hint` (also Ctrl-D) |
| test_permission_fallback_denies | dropped: the Python modal was an optional import; the Rust modal is always built |
| test_at_completion | `composer::at_completion_finds_files` (Python also keeps the `@`; so do we) |
| test_engine_calls_that_raise_become_notices | `app::engine_trouble_becomes_notices_not_crashes` (no session, `Error`, malformed line, closed socket) |
| test_mode_auto | `app::model_and_mode_commands...` (Shift+Tab cycles to `auto`) |
| test_copy_last_reply | `app::copy_puts_the_last_reply_on_the_clipboard_effect` (OSC 52 + wl-copy/xclip in `run.rs`, `util::base64` tested against RFC 4648 vectors) |
| test_reload_keeps_session_and_transcript, test_reload_refuses_syntax_errors_and_busy | dropped, because: `/reload` re-imports Python modules; a compiled binary has nothing to reload (restart reattaches to the live engine session: `--attach`) |
| test_tool_card_title_survives_bracket_runs | `transcript::tool_card_title_survives_bracket_runs` (no markup layer, so nothing to escape; pinned anyway) |
| test_system_clipboard_ignores_non_text | implemented (`util::system_clipboard` returns "" on a failing tool or non-UTF-8), NOT unit-tested: it shells out to wl-paste/xclip |

## test_ui_transcript_scroll.py
| Python test | Rust |
|---|---|
| stray_jump_to_top_while_blurred_is_repinned | `transcript::stray_jump_to_top_while_blurred_is_repinned` |
| stray_jump_right_after_refocus_is_repinned | `transcript::stray_jump_right_after_refocus_is_repinned` (400 ms guard, then the reader may scroll) |
| reader_scrolled_up_is_not_dragged_down | `transcript::follows_while_pinned_and_stays_put_when_scrolled_up`, `a_reader_scrolled_up_stays_up_across_blur` |
| nudged_two_lines_up_still_follows | `transcript::nudged_two_lines_up_still_follows...` (slack: distance < 3 lines) |
| submit_jumps_to_bottom_after_scrolling_up | `transcript::submit_jumps_to_bottom_after_scrolling_up`, `app::own_messages_jump_to_the_bottom_others_do_not_yank_the_reader` |
| wheel_burst_accumulates_into_one_glide | dropped, because: Mark chose 1:1 instant scroll (Vim feel). Replaced by `transcript::wheel_is_one_to_one_three_lines_per_notch` and `app::mouse_wheel_scrolls_one_to_one...`. `glide.rs` stays only for the R0 spike and the bench crate. |

## test_ui_permission.py
| Python test | Rust |
|---|---|
| allow_keys, allow_session, deny_with_reason, deny_empty_reason, escape_denies | `modal::{allow_keys, allow_session, deny_with_reason, deny_empty_reason, escape_denies}` |
| bash_command_not_truncated, edit_diff, organ_label | `modal::{bash_command_is_not_truncated, edit_shows_a_diff, the_organ_is_named}` |
| picker_enter / down_enter / escape / empty | `modal::picker_enter_down_enter_escape_and_empty` |
| no_hex_literals | `theme::no_hex_or_rgb_literals_anywhere_in_src` (walks every file under `src/`, fails if the walk finds too few; also rejects `Color::Rgb(`/`Color::Indexed(` outside `theme.rs`). The R0 spike's RGB literals now come from `Theme`. |
| keys_inside_the_arming_window_are_ignored | `modal::keys_inside_the_arming_window_are_ignored_but_escape_is_not`, `modal::continuous_typing_never_answers_even_past_the_arming_window` (STRONGER than Python: every ignored key pushes the deadline out, so a typist never answers; Enter is not an allow key; key repeats are ignored: `app::held_keys_cannot_answer_a_prompt...`) |
| answer_controls_stay_on_screen_in_a_short_pane | `modal::answer_controls_and_reason_stay_on_screen_in_a_short_pane` (8, 10, 16, 24, 40 rows; the click half is dropped: the Rust prompt is keyboard only). Differs on purpose: a body taller than the pane scrolls (Up/Down/PageUp/PageDown) and the allow keys stay dead until the end has been shown (`a_clipped_body_cannot_be_allowed_until_it_has_been_read`). |
| new (multi-client) | `app::permission_ask_answer_and_dismissal_by_another_client` (PermissionResolved closes the prompt, a replayed ask is not reopened) |

## test_ui_prefs.py
| Python test | Rust |
|---|---|
| settings_roundtrip_and_bad_file | `prefs::settings_roundtrip_and_bad_file` (same `ui.json`, same fields, same fallbacks) |
| resolve_model | `prefs::resolve_model_maps_labels_and_passes_ids_through` |
| model_crew_trust_commands | `app::model_trust_crew_commands` (`/crew` restarts via `create_session{resume,crew}`) |
| slash_completion_and_help | `app::slash_completion_uses_capabilities_and_help_lists_commands`, `composer::slash_completion_*` |

## test_ui_theme.py
| Python test | Rust |
|---|---|
| palette_parses | `rabble-theme` tests (Lane-shared crate) |
| rabble_theme | `theme::colour_comes_from_the_palette` |
| no_hex_literals | `theme::no_hex_or_rgb_literals_anywhere_in_src` |
| organ_colors | CHANGED: Python reads each persona's `color_base` from the Crew frontmatter; Rust hashes the organ name onto palette accents (`Theme::organ`). Persona colours are unfinished (needs the roster parsed in Rust or shipped by the engine). |
| new | `theme::no_color_drops_every_colour_but_keeps_emphasis`, `vscode_gets_safe_glyphs`, `app::nothing_the_shell_draws_needs_a_hex...` (NO_COLOR leaves no colour cells) |

## Behaviours in `ui/` with no Python test
Ported: header (model, mode, trust, crew, cwd, entropy, cost, context %, limit), divider with status, thinking placeholder, Ctrl-O tool cards, Shift+Tab mode cycle, history, multiline (Shift+Enter, Ctrl-J, Alt-Enter), bracketed paste, Ctrl-V clipboard paste, `/park` settings, `/park now|continue`, auto-park at `RABBLE_PARK_AT` (default 80%), handoff check, `/resume` picker (live engine sessions + `~/.claude/projects` history), `/status`, `/help`.
Dropped, because: `/reload` (compiled); Ctrl-C copies a mouse selection (mouse capture is on, the terminal selects with Shift+drag, so bare Ctrl-C only counts toward quitting); park ETA from context growth (`Bloat`); the "inside RaBbLE-sCoRE" warning; the OS usage tracker (5h/week windows in the header and `/status`), which reads `orchestra/usage.py`'s cache and needs a port or an engine event.
Not here (other lanes): the face widget (empty slot: `widgets::FACE_SLOT_WIDTH`, `/face` says so), narration lines (Lane N events render as `Unknown` until `rabble-proto` adds them).

## Added by the S296 review fixes (no Python counterpart)
Prompt: ask arriving behind a picker (`an_ask_that_arrives_while_a_picker_is_open...`), late `/resume` list never replaces a prompt and switching sessions clears queued asks (`a_late_session_list...`), tiny-terminal modals (`modals_do_not_panic_in_tiny_areas`).
Terminal: half frame discarded on panic (`run::a_half_built_frame...`), ESU first in restore (`run::restore_ends_synchronized_output...`), SIGTERM and SIGHUP restore the terminal (checked by hand in a pty), input-thread failure quits with a message.
Engine I/O: bounded frame queue with the input channel drained first, capped line reader, invalid UTF-8 is malformed not closed (`client::` tests), reconnect with `attach{since_seq: max(seq)}` (`app::a_dropped_connection_reconnects...`), a replaced `create_session` is closed not adopted, another session's `Error` is not shown.
Workers off the render thread: `@` file scan (capped during the walk, no symlinked dirs, cache dropped on submit), history titles (first 200 lines of at most 4 MiB), Ctrl-V clipboard (2 s limit), session list.
Safety of what is drawn: control characters, C1, DEL and bidi overrides are stripped, tabs expanded (`wrap::clean`; `transcript::escape_sequences_from_the_engine_never_reach_a_cell`, `modal::escape_sequences_in_a_prompt...`, `composer::submit_drops_the_file_cache_and_paste_strips_escapes`).
