"use client";

import { Plus, X } from "lucide-react";
import {
  type FocusEvent,
  type ReactNode,
  useRef,
  useState,
} from "react";

interface FilterChipPopoverProps {
  /** "chip" edits an active filter; "add" starts a new one from a dashed button. */
  variant?: "chip" | "add";
  chipLabel?: string;
  /** The chip's value, or the dashed button's label in the add variant. */
  summary: string;
  title: string;
  hint: string;
  disabled?: boolean;
  removeLabel?: string;
  /** Left footer control, shown only alongside `onRemove`: there is nothing to clear without it. */
  clearActionLabel?: string;
  /** Omit both to commit on choice; the footer then carries only the clear action. */
  confirmLabel?: ReactNode;
  confirmDisabled?: boolean;
  /** Fired as the popover opens, so the body can reset its drafts and take focus. */
  onOpen: () => void;
  /**
   * Fired as the popover is left, which is how a body that stays open commits.
   *
   * Not fired when the body closed itself (it has already committed) or when the filter was
   * removed (there is nothing left to commit to). Must be idempotent: leaving without touching
   * anything has to be a no-op.
   */
  onDismiss?: () => void;
  onConfirm?: () => void;
  /** Chip variant only; the add variant has nothing to remove yet. */
  onRemove?: () => void;
  /** A function body receives `close`, which is how a choice commits and dismisses. */
  children: ReactNode | ((close: () => void) => ReactNode);
}

/**
 * The chrome every filter chip shares: a trigger, an anchored popover, and a
 * footer.
 *
 * Editing lives here rather than in each filter's own component so that
 * dismissal and focus return cannot drift between a list of places and a price
 * comparison. A choice commits as it is made, so the footer's primary control
 * is optional: a body that has nothing left to confirm simply omits it.
 */
export function FilterChipPopover({
  variant = "chip",
  chipLabel,
  summary,
  title,
  hint,
  disabled = false,
  removeLabel,
  clearActionLabel,
  confirmLabel,
  confirmDisabled = false,
  onOpen,
  onDismiss,
  onConfirm,
  onRemove,
  children,
}: FilterChipPopoverProps) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const clearable = Boolean(onRemove && clearActionLabel);
  const confirmable = confirmLabel !== undefined && Boolean(onConfirm);

  const openPopover = () => {
    onOpen();
    setOpen(true);
  };

  /** Leaving is what commits a body that stayed open while it was being edited. */
  const leave = () => {
    onDismiss?.();
    setOpen(false);
  };

  /** Closing without committing: the body already applied, or the filter is gone. */
  const closeQuietly = () => setOpen(false);

  /** Escape leaves from anywhere in the popover, not just a field. */
  const dismiss = () => {
    leave();
    triggerRef.current?.focus();
  };

  const handleBlur = (event: FocusEvent<HTMLDivElement>) => {
    const control = event.currentTarget;
    window.setTimeout(() => {
      if (!control.contains(document.activeElement)) leave();
    }, 0);
  };

  return (
    <div
      className={[
        "filter-chip-editor",
        variant === "chip" ? "active-filter-chip" : "filter-chip-editor--add",
      ].join(" ")}
      onBlur={handleBlur}
    >
      {/* Ahead of the body: dropping a filter is the action reached most often, and a strip of
          chips puts a leading control in one column the eye can run down. */}
      {variant === "chip" && onRemove ? (
        <button
          className="active-filter-chip__remove"
          type="button"
          aria-label={removeLabel}
          onClick={() => {
            onRemove();
            closeQuietly();
          }}
        >
          <X aria-hidden="true" />
        </button>
      ) : null}

      <button
        ref={triggerRef}
        className={variant === "chip"
          ? "active-filter-chip__body filter-chip-editor__trigger"
          : "active-filter-add filter-chip-editor__trigger--add"}
        type="button"
        disabled={disabled}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => (open ? leave() : openPopover())}
      >
        {variant === "chip" ? (
          <>
            <small>{chipLabel}</small>
            <strong>{summary}</strong>
          </>
        ) : (
          <>
            <Plus aria-hidden="true" />
            {summary}
          </>
        )}
      </button>

      {open ? (
        <div
          className="filter-chip-editor__popover"
          role="dialog"
          aria-label={title}
          onKeyDown={(event) => {
            if (event.key !== "Escape") return;
            event.preventDefault();
            event.stopPropagation();
            dismiss();
          }}
        >
          <div className="filter-chip-editor__mode">
            <span>{title}</span>
            <small>{hint}</small>
          </div>

          {typeof children === "function" ? children(closeQuietly) : children}

          {/* A footer only when it carries something. Nothing is staged here any more, so a
              popover that starts a new filter has nothing to cancel: leaving is how you leave. */}
          {clearable || confirmable ? (
            <footer>
              {clearable ? (
                <button
                  type="button"
                  onClick={() => {
                    onRemove?.();
                    closeQuietly();
                  }}
                >
                  {clearActionLabel}
                </button>
              ) : null}
              {confirmable ? (
                <button
                  className="is-primary"
                  type="button"
                  disabled={confirmDisabled}
                  onClick={() => {
                    onConfirm?.();
                    closeQuietly();
                  }}
                >
                  {confirmLabel}
                </button>
              ) : null}
            </footer>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
