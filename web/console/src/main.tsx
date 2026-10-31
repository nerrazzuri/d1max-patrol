import "./fonts.css";
import "./styles.css";
import { render } from "preact";
import { App } from "./app";
import { installErrorLog } from "./errlog";
import { lang, setLang } from "./i18n";
import { followSystem } from "./theme";

followSystem();
installErrorLog();
setLang(lang.value);
render(<App />, document.getElementById("app")!);
