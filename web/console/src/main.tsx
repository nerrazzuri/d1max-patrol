import "@fontsource/atkinson-hyperlegible-next/latin-400.css";
import "@fontsource/atkinson-hyperlegible-next/latin-600.css";
import "@fontsource/atkinson-hyperlegible-next/latin-700.css";
import "@fontsource/atkinson-hyperlegible-mono/latin-500.css";
import "@fontsource/atkinson-hyperlegible-mono/latin-600.css";
import "./styles.css";
import { render } from "preact";
import { App } from "./app";
import { lang, setLang } from "./i18n";
import { applyTokens } from "./tokens";

applyTokens();
setLang(lang.value);
render(<App />, document.getElementById("app")!);
