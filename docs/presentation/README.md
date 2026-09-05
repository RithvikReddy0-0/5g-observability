# Phase 2 Review 1 presentation

`phase2-review1.tex` — the review deck, in LaTeX Beamer. `phase2-review1.pdf` is the
compiled output (19 slides).

## Why LaTeX rather than PowerPoint

The previous deck was rejected for inconsistent fonts and formatting. In Beamer the font
family, sizes, colours and spacing are set once in the preamble and applied by the document
class, so they cannot drift slide to slide — the defect is designed out rather than
proof-read out. Diagrams are drawn in TikZ instead of pasted as screenshots, so they stay
sharp at any projector resolution and can be edited as text.

The deck also carries an acknowledgement of AI assistance in the footer of every slide, as
required.

## Structure

Follows the department's approval-presentation template:

1. Title · 2. Guide's approval · 3. Problem definition · 4–5. Literature survey ·
6. Justification · **7. Objectives and current status** · 8. Architecture ·
9. Functionalities · 10. Software/tools · 11–13. Phase 2 progress · 14. Results ·
15. Challenges · 16. Timeline · 17. Conclusion · 18. References · 19. Thank you

The objectives slide sits **after** the literature survey, on the guide's instruction, and
states how much of each objective is done, what is left, and the principal challenge —
rather than restating the original proposal.

## Rebuilding

Either upload `phase2-review1.tex` and `assets/` to Overleaf and press Recompile, or build
locally with any TeX Live that has `beamer`, `booktabs` and `tikz`:

```bash
cd docs/presentation && pdflatex -interaction=nonstopmode phase2-review1.tex
```

Run it twice so the slide-count in the footer resolves.

## Before presenting

Slide 2 contains a **placeholder** for the screenshot of the guide's approval mail. Replace
the framed box with the image.
