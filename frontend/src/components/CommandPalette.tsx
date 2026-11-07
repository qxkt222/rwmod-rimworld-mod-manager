/**
 * Ctrl+K command palette (docs/ui-redesign.md §5.1).
 *
 * Keeps the legacy DOM contract — `#cmd-palette` + `.open`, `#cmd-input`,
 * `.cmd-result` + `.selected` — so `command-palette.spec.ts` keeps passing.
 * New code targets `dialog-command-palette`.
 */
import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { createPortal } from "react-dom";

import { filterCommands } from "../app/commands";
import { useUIStore } from "../store/ui";

export function CommandPalette() {
  const open = useUIStore((state) => state.commandPaletteOpen);
  const setOpen = useUIStore((state) => state.setCommandPalette);

  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const previousFocus = useRef<HTMLElement | null>(null);

  const filtered = useMemo(() => filterCommands(query), [query]);

  useEffect(() => {
    if (open) {
      previousFocus.current = document.activeElement as HTMLElement | null;
      setQuery("");
      setSelected(0);
      inputRef.current?.focus();
    } else {
      previousFocus.current?.focus?.();
    }
  }, [open]);

  const close = () => setOpen(false);

  const run = (index: number) => {
    const command = filtered[index];
    if (!command) return;
    close();
    void command.run();
  };

  const move = (delta: number) => {
    setSelected((current) => {
      if (filtered.length === 0) return 0;
      return Math.min(Math.max(current + delta, 0), filtered.length - 1);
    });
  };

  const onKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (!open) return;
    switch (event.key) {
      case "Escape":
        event.preventDefault();
        event.stopPropagation();
        close();
        break;
      case "ArrowDown":
        event.preventDefault();
        move(1);
        break;
      case "ArrowUp":
        event.preventDefault();
        move(-1);
        break;
      case "Enter":
        event.preventDefault();
        run(selected);
        break;
      case "Tab":
        // Single focusable control inside the palette — keep focus trapped.
        event.preventDefault();
        inputRef.current?.focus();
        break;
      default:
        break;
    }
  };

  return createPortal(
    <div
      id="cmd-palette"
      className={`cmd-overlay${open ? " open" : ""}`}
      data-testid="dialog-command-palette"
      role="dialog"
      aria-modal={open ? true : undefined}
      aria-hidden={!open}
      aria-label="命令面板"
      onKeyDown={onKeyDown}
    >
      <div className="cmd-backdrop" onClick={close} />
      <div className="cmd-box">
        <input
          id="cmd-input"
          ref={inputRef}
          className="cmd-input"
          type="text"
          placeholder="输入命令..."
          autoComplete="off"
          aria-label="搜索命令"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            setSelected(0);
          }}
        />
        <div id="cmd-results" className="cmd-results" role="listbox" aria-label="命令列表">
          {filtered.map((command, index) => (
            <div
              key={command.id}
              id={`cmd-result-${command.id}`}
              className={`cmd-result${index === selected ? " selected" : ""}`}
              data-idx={index}
              role="option"
              aria-selected={index === selected}
              onMouseEnter={() => setSelected(index)}
              onClick={() => run(index)}
            >
              <span aria-hidden="true">{command.icon}</span>
              <span>{command.label}</span>
              {command.shortcut ? <span className="cmd-kbd">{command.shortcut}</span> : null}
            </div>
          ))}
          {filtered.length === 0 ? <div className="cmd-empty">没有匹配的命令</div> : null}
        </div>
      </div>
    </div>,
    document.body,
  );
}

export default CommandPalette;
