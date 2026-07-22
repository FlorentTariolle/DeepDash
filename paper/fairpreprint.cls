% De-branded FAIR-style preprint class.
% Based on fairmeta.cls v1.1 (Meta FAIR) and the public template used by
% MIRA, arXiv:2607.05352. Meta-only logo and Optimistic font are omitted;
% TeX Gyre Heros provides the portable modern sans-serif display face.

\NeedsTeXFormat{LaTeX2e}
\ProvidesClass{fairpreprint}[2026/07/22 FAIR-style public preprint]
\LoadClassWithOptions{article}

% Layout.
\RequirePackage[top=2.25cm,bottom=2.5cm,left=2.5cm,right=2.5cm,columnsep=0.65cm]{geometry}
\RequirePackage{microtype}
\RequirePackage{placeins}
\RequirePackage{hyphenat}
\RequirePackage{setspace}
\RequirePackage{parskip}
\RequirePackage[latin,english]{babel}
\RequirePackage{etoolbox}
\emergencystretch=2em

% Graphics and tables.
\RequirePackage{graphicx}
\RequirePackage{subcaption}
\RequirePackage{booktabs}
\RequirePackage{multirow}
\RequirePackage{bm}

% FAIR-style palette and title enclosure.
\RequirePackage[most]{tcolorbox}
\RequirePackage{xcolor}
\definecolor{fairblue}{HTML}{2563EB}
\definecolor{fairtitlefg}{HTML}{1C2B33}
\definecolor{fairtitlebg}{HTML}{F2F3F4}

% References.
\RequirePackage{hyperref}
\RequirePackage[noabbrev,nameinlink]{cleveref}
\hypersetup{
  colorlinks=true,
  linkcolor=fairblue,
  citecolor=fairblue,
  urlcolor=fairblue
}
\RequirePackage[round,authoryear]{natbib}
\def\bibfont{\small}

% Portable modern display typography. The public build uses XeLaTeX.
\RequirePackage{iftex}
\ifPDFTeX
  \RequirePackage[T1]{fontenc}
  \RequirePackage{lmodern}
  \newcommand{\fairtitlefont}{\sffamily}
\else
  \RequirePackage{fontspec}
  \newfontfamily\fairtitlefont{texgyreheros}[
    Extension=.otf,
    UprightFont=*-regular,
    BoldFont=*-bold,
    ItalicFont=*-italic,
    BoldItalicFont=*-bolditalic
  ]
\fi

% Section and caption styling.
\RequirePackage{titlesec}
\titleformat*{\paragraph}{\bfseries}
\titleformat*{\section}{\Large\fairtitlefont\bfseries}
\titleformat*{\subsection}{\large\fairtitlefont\bfseries}
\titleformat*{\subsubsection}{\normalsize\fairtitlefont\bfseries}

\RequirePackage{caption}
\DeclareCaptionLabelSeparator{faircustom}{}
\DeclareCaptionFormat{faircustom}{{\fairtitlefont\textbf{#1 #2}} #3}
\captionsetup{singlelinecheck=false,format=faircustom,labelsep=faircustom,font=small}
\captionsetup[sub]{singlelinecheck=true,format=faircustom,labelsep=faircustom,font=small}

% Semantic title metadata.
\newcommand\fairaddtolist[5][]{%
  \begingroup
    \if\relax#3\relax\def\fairsep{}\else\def\fairsep{#5}\fi
    \let\protect\@unexpandable@protect
    \xdef#3{\expandafter{#3}\fairsep #4[#1]{#2}}%
  \endgroup
}

\newcommand\authorlist{}
\newcommand\fairauthorformat[2][]{{\fairtitlefont\bfseries #2$^{#1}$}}
\renewcommand\author[2][]{\fairaddtolist[#1]{#2}{\authorlist}{\fairauthorformat}{, }}

\newcommand\affiliationlist{}
\newcommand\fairaffiliationformat[2][]{{\normalsize $^{#1}$#2}}
\newcommand\affiliation[2][]{\fairaddtolist[#1]{#2}{\affiliationlist}{\fairaffiliationformat}{, }}

\newcommand\contributionlist{}
\newcommand\faircontributionformat[2][]{{\small $^{#1}$#2}}
\newcommand\contribution[2][]{\fairaddtolist[#1]{#2}{\contributionlist}{\faircontributionformat}{, }}

\newcommand\metadatalist{}
\newcommand\fairmetadataformat[2][]{{\small {\fairtitlefont\bfseries #1:} #2}}
\newcommand\metadata[2][]{\fairaddtolist[#1]{#2}{\metadatalist}{\fairmetadataformat}{\par}}

\newcommand\abstractlist{}
\renewcommand{\abstract}[1]{\gdef\abstractlist{{\color{fairtitlefg}#1}}}
\newcommand{\email}[1]{\href{mailto:#1}{\texttt{#1}}}
\renewcommand\date[1]{\metadata[Date]{#1}}
\newcommand\correspondence[1]{\metadata[Correspondence]{#1}}
\renewcommand{\title}[1]{\gdef\titlelist{{\huge\fairtitlefont\bfseries #1}}}

% Rounded title, authors, abstract, and resource metadata card.
\newcommand{\fairmaketitle}{%
  \begin{tcolorbox}[
    enhanced,
    frame hidden,
    colback=fairtitlebg,
    arc=10pt,
    boxsep=0pt,
    left=0.5cm,right=0.5cm,top=0.5cm,bottom=0.5cm,
    before skip=0pt,
    grow to left by=1.5pt,
    grow to right by=1.5pt
  ]
    \setlength{\parindent}{0pt}
    \setlength{\parskip}{0.5cm}
    {\setlength{\parskip}{0pt}\raggedright\nohyphens
      {\setstretch{1.5}\titlelist\par}
      \vskip 0.2cm
      \authorlist\par
      \vskip 0.2cm
      \affiliationlist\par
      \contributionlist\par
    }
    \abstractlist\par
    \vskip 0.5cm
    {\setlength{\parskip}{0pt}%
      \ifdefempty{\metadatalist}{\vspace*{0.65cm}}{\metadatalist\par}}
  \end{tcolorbox}
  \FloatBarrier
}

\if@twocolumn
  \renewcommand{\maketitle}{\twocolumn[\fairmaketitle\vskip 0.38cm]}
\else
  \renewcommand{\maketitle}{\fairmaketitle}
\fi
