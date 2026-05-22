"""Shared resume theme defaults and presets."""

DEFAULT_OH_MY_CV_RESUME_CSS = """
#resume-preview [data-scope="vue-smart-pages"][data-part="page"] {
  background-color: white;
  color: #161616;
  font-family: "Georgia", "Times New Roman", serif;
  font-size: 10.9pt;
  text-align: left;
  letter-spacing: -0.003em;
  text-rendering: optimizeLegibility;
  -moz-hyphens: auto;
  -ms-hyphens: auto;
  -webkit-hyphens: auto;
  hyphens: auto;
}

#resume-preview p,
#resume-preview li {
  margin: 0;
  line-height: 1.4;
}

#resume-preview h1 {
  margin: 0 0 7px 0;
  color: #3f8a3b;
  font-family: "Palatino Linotype", "Book Antiqua", "Georgia", serif;
  font-size: 2.34em;
  font-weight: 700;
  letter-spacing: -0.03em;
}

#resume-preview h2 {
  margin: 15px 0 6px 0;
  color: #3f8a3b;
  font-family: "Palatino Linotype", "Book Antiqua", "Georgia", serif;
  font-size: 1.46em;
  font-weight: 700;
  letter-spacing: -0.015em;
  border-bottom: 1px solid rgba(63, 138, 59, 0.75);
  padding-bottom: 3px;
}

#resume-preview h3 {
  margin-bottom: 4px;
  font-size: 1.08em;
}

#resume-preview ul,
#resume-preview ol {
  padding-left: 1.16em;
  margin: 0.22em 0 0.5em 0;
}

#resume-preview ul {
  list-style-type: circle;
  list-style-position: outside;
}

#resume-preview .resume-header {
  text-align: center;
  margin-bottom: 11px;
}

#resume-preview .resume-header h1 {
  text-align: center;
  line-height: 0.98;
}

#resume-preview .resume-header-row {
  margin: 0;
  line-height: 1.26;
}

#resume-preview .resume-header-row-primary {
  font-family: "Helvetica Neue", "Arial", sans-serif;
  font-size: 0.99em;
  font-weight: 600;
  color: #111;
  margin-bottom: 2px;
}

#resume-preview .resume-header-row-secondary {
  font-family: "Helvetica Neue", "Arial", sans-serif;
  font-size: 0.93em;
  color: #333;
}

#resume-preview .resume-header-item:not(.no-separator)::after {
  content: " | ";
  color: rgba(63, 138, 59, 0.7);
}

#resume-preview .resume-header-item a {
  color: inherit;
  text-decoration: none;
}

#resume-preview .resume-entry {
  margin: 0 0 8px 0;
}

#resume-preview .resume-entry-title,
#resume-preview .resume-entry-definition dt,
#resume-preview .resume-entry-definition dd {
  font-family: "Helvetica Neue", "Arial", sans-serif;
}

#resume-preview .resume-entry-definition dt strong,
#resume-preview .resume-entry-title strong {
  font-size: 1.01em;
}

#resume-preview .resume-skill-line strong {
  font-family: "Helvetica Neue", "Arial", sans-serif;
}

#resume-preview code {
  font-family: "SFMono-Regular", "Consolas", monospace;
}
""".strip()
