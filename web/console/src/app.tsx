import { useEffect, useState } from "preact/hooks";
import { api, ApiError, onUnauthorized } from "./api";
import { Console } from "./console";
import { Login } from "./login";

export interface Me {
  name: string;
  role: string;
  display_name: string;
  site_name: string;
}

export function App() {
  const [me, setMe] = useState<Me | null | undefined>(undefined);
  const [expired, setExpired] = useState(false);

  useEffect(() => {
    onUnauthorized.cb = () => {
      setExpired(true);
      setMe(null);
    };
    api<Me>("GET", "/api/me")
      .then(setMe)
      .catch((e) => {
        if (!(e instanceof ApiError) || e.status !== 401) console.warn(e);
        setMe(null);
      });
  }, []);

  if (me === undefined) return <div class="boot" aria-busy="true" />;
  if (me === null)
    return (
      <Login
        expired={expired}
        onIn={(m) => {
          setExpired(false);
          setMe(m);
        }}
      />
    );
  return <Console me={me} onOut={() => setMe(null)} />;
}
