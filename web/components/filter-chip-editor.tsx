"use client";

import { Check, Search, X } from "lucide-react";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { FilterChipPopover } from "@/components/filter-chip-popover";

export interface FilterChipEditorOption {
  id: string;
  label: string;
  /** Right-aligned hint, usually a facet count. */
  detail?: string;
  /** Section heading. Sections render in first-appearance order. */
  group?: string;
}

interface FilterChipEditorProps {
  variant?: "chip" | "add";
  chipLabel?: string;
  summary: string;
  title: string;
  hint: string;
  options: FilterChipEditorOption[];
  /** Empty in the add variant: nothing is chosen until the reader picks. */
  selectedIds?: string[];
  /** Whether a save can carry several options at once. */
  multiple?: boolean;
  searchLabel: string;
  emptyLabel?: string;
  clearActionLabel?: string;
  removeLabel?: string;
  disabled?: boolean;
  onApply: (optionIds: string[]) => void;
  onRemove?: () => void;
}

/**
 * A filter chip whose value is chosen from a list.
 *
 * Chips used to hand editing back to the search box, which meant changing
 * "San Francisco" to "Oakland" was a retype rather than a pick. Picking is now
 * the whole interaction: a choice from this list is a complete answer, so it
 * applies as it is made and the popover has nothing left to confirm.
 *
 * A single-select closes on the pick, because the question it asked is
 * answered, and nothing has moved by the time it is gone.
 *
 * A multi-select stays open, and there applying each pick as it lands is what
 * makes the popover unusable: every new chip widens the strip, which shifts the
 * trigger this popover is anchored to, so the list slides out from under the
 * pointer between one pick and the next. So picks are held here and applied in
 * one transaction when the reader leaves -- nothing outside the popover moves
 * while it is open.
 */
export function FilterChipEditor({
  variant = "chip",
  chipLabel,
  summary,
  title,
  hint,
  options,
  selectedIds = [],
  multiple = false,
  searchLabel,
  emptyLabel = "No matches",
  clearActionLabel,
  removeLabel,
  disabled = false,
  onApply,
  onRemove,
}: FilterChipEditorProps) {
  const [term, setTerm] = useState("");
  const [draftIds, setDraftIds] = useState<string[]>(selectedIds);
  const [openedAt, setOpenedAt] = useState(0);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const optionsRef = useRef<HTMLDivElement>(null);

  // A long list — every city we carry — opens on whatever is already chosen,
  // so the popover answers "what is this filter set to" before it is edited.
  useEffect(() => {
    if (!openedAt) return;
    const list = optionsRef.current;
    const selected = list?.querySelector<HTMLElement>('[aria-selected="true"]');
    if (!list || !selected) return;
    list.scrollTop = Math.max(
      0,
      selected.offsetTop - (list.clientHeight - selected.clientHeight) / 2,
    );
  }, [openedAt]);

  const visible = useMemo(() => {
    const needle = term.trim().toLocaleLowerCase();
    if (!needle) return options;
    return options.filter((option) => (
      option.label.toLocaleLowerCase().includes(needle)
      || (option.group?.toLocaleLowerCase().includes(needle) ?? false)
    ));
  }, [options, term]);

  // Picks scatter through a long list, so what is about to be added is restated
  // as a row above it.
  const picked = useMemo(
    () => draftIds.flatMap((id) => {
      const option = options.find((candidate) => candidate.id === id);
      return option ? [option] : [];
    }),
    [draftIds, options],
  );

  const groups = useMemo(() => {
    const order: string[] = [];
    const byGroup = new Map<string, FilterChipEditorOption[]>();
    for (const option of visible) {
      const key = option.group ?? "";
      const bucket = byGroup.get(key);
      if (bucket) {
        bucket.push(option);
      } else {
        byGroup.set(key, [option]);
        order.push(key);
      }
    }
    return order.map((key) => ({ key, options: byGroup.get(key) ?? [] }));
  }, [visible]);

  const openEditor = () => {
    setTerm("");
    setDraftIds(selectedIds);
    setOpenedAt((current) => current + 1);
    window.requestAnimationFrame(() => searchInputRef.current?.focus());
  };

  /**
   * A pick is the answer, so it applies rather than staging.
   *
   * Single-select replaces the value and dismisses; multi-select adds one and
   * stays open, because the caller's option list is "what is left to add" and
   * shrinks under the pointer as picks land.
   */
  const choose = (optionId: string, close: () => void) => {
    if (!multiple) {
      setDraftIds([optionId]);
      onApply([optionId]);
      close();
      return;
    }
    setDraftIds((current) => (
      current.includes(optionId)
        ? current.filter((candidate) => candidate !== optionId)
        : [...current, optionId]
    ));
    searchInputRef.current?.focus();
  };

  /** Leaving applies what was picked. Nothing picked is nothing to apply. */
  const commitPending = () => {
    if (!multiple || !draftIds.length) return;
    onApply(draftIds);
    setDraftIds([]);
  };

  return (
    <FilterChipPopover
      variant={variant}
      chipLabel={chipLabel}
      summary={summary}
      title={title}
      hint={hint}
      disabled={disabled}
      removeLabel={removeLabel}
      clearActionLabel={clearActionLabel}
      onOpen={openEditor}
      onDismiss={commitPending}
      onRemove={onRemove}
    >
      {(close) => (
        <>
      <label className="filter-chip-editor__search">
        <Search aria-hidden="true" />
        <span className="sr-only">{searchLabel}</span>
        <input
          ref={searchInputRef}
          value={term}
          placeholder={searchLabel}
          maxLength={60}
          onChange={(event) => setTerm(event.target.value)}
          onKeyDown={(event) => {
            if (event.key !== "Enter") return;
            event.preventDefault();
            const topMatch = term.trim() && visible.length ? visible[0].id : "";
            if (!topMatch) return;
            // Enter takes the top match, which is the same commitment a click is.
            choose(topMatch, close);
            setTerm("");
          }}
        />
      </label>

      {multiple && picked.length ? (
        <div className="filter-chip-editor__picks">
          {picked.map((option) => (
            <button
              key={option.id}
              type="button"
              aria-label={`Unpick ${option.label}`}
              onClick={() => choose(option.id, close)}
            >
              {option.label}
              <X aria-hidden="true" />
            </button>
          ))}
        </div>
      ) : null}

      <div
        className="filter-chip-editor__options"
        ref={optionsRef}
        role="listbox"
        aria-label={title}
        aria-multiselectable={multiple || undefined}
      >
        {groups.map((group) => (
          <div
            className="filter-chip-editor__group"
            key={group.key || "ungrouped"}
            role="group"
            aria-label={group.key || undefined}
          >
            {group.key ? <span>{group.key}</span> : null}
            {group.options.map((option) => (
              <button
                key={option.id}
                className={draftIds.includes(option.id) ? "is-active" : ""}
                type="button"
                role="option"
                aria-selected={draftIds.includes(option.id)}
                onClick={() => choose(option.id, close)}
              >
                <Check aria-hidden="true" />
                <strong>{option.label}</strong>
                {option.detail ? <small>{option.detail}</small> : null}
              </button>
            ))}
          </div>
        ))}
        {visible.length ? null : (
          <p className="filter-chip-editor__empty">{emptyLabel}</p>
        )}
      </div>

        </>
      )}
    </FilterChipPopover>
  );
}
