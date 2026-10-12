# Licences and the shipped image

## This repository
Apache License 2.0 (`LICENSE`). The fixture's lettering was converted to outlines from
two fonts under the SIL Open Font License 1.1 (Cormorant Garamond and Jost); see
`fixtures/source/OFL-fonts.txt`. No font file is shipped.

## What the image contains
The exact versions installed in the image are written to `/app/RUNTIME_VERSIONS.txt`
at build time; the copy recorded with the goldens is `goldens/RUNTIME_VERSIONS.txt`.
At the time of recording (Debian 12 packages, pinned base image digest in `Dockerfile`):

| Component | Version | Licence | How it is used here |
|---|---|---|---|
| Ghostscript (`ghostscript`, `libgs10`) | 10.0.0~dfsg-11+deb12u8 | AGPL-3.0 or commercial licence from its publisher | separate process: `gs -sDEVICE=bbox` reads the plate extent |
| Poppler (`poppler-utils`, `libpoppler126`) | 22.12.0-2+deb12u3 | GPL-2.0 or GPL-3.0 | separate processes: `pdfimages`, `pdftoppm` |
| Cairo (`libcairo2`) | 1.16.0-7 | LGPL-2.1 or MPL-1.1 | linked through CairoSVG/cairocffi |
| LittleCMS (`liblcms2-2`) | 2.14-2+deb12u1 | MIT | system library; Pillow ships its own 2.19 |
| CairoSVG | 2.7.1 | LGPL-3.0 | Python library |
| Pillow | 12.3.0 | MIT-CMU | Python library |
| Python packages | see `requirements.txt` | various | |

## The Ghostscript question is a task, not a footnote
The AGPL covers network use of a *modified* covered program (section 13) and the
distribution of binaries. What applies to a real prepress service depends on facts
this reference cannot settle:

1. **Integration.** Here Ghostscript runs unmodified as a separate process. Whether a
   given production engine forms a combined work with it depends on how it is called
   and whether it is modified or linked.
2. **Distribution.** The image lives in a private registry and is not distributed. If
   an image is ever handed to a third party, the matching source for the AGPL and GPL
   components must be offered with it (Debian source packages for the versions above),
   with the licence notices.
3. **Route.** Choose one, in writing, before production: comply with the AGPL for the
   way it is used; buy a commercial licence from the publisher; or replace the
   Ghostscript calls (for example, the extent check can be done with Poppler or pikepdf).

None of this means a private engine must be published. It means the decision belongs
in discovery, with the actual call sites in front of whoever signs it off.

## Data leaving the system
This reference calls no outside API. A real engine that does should have an explicit,
written list of which processors receive which data, so that a data-residency
rule is something the code can be checked against.
