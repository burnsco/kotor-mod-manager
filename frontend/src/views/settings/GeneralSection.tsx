import { useEffect, useState } from "react";
import { FolderOpen } from "lucide-react";
import { api, type Settings } from "@/lib/api";
import { pickDirectory } from "@/lib/tauri";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useLanguage, LOCALES, type Locale } from "@/lib/i18n";
import { isUiSoundEnabled, setUiSoundEnabled, playClick } from "@/lib/sound";

const EMPTY: Settings = { kotor1_path: "", kotor2_path: "", download_dir: "", language: "en", custom_patcher_path: "", nexus_api_key: "", preferred_resolution: "", browser_download_mode: true };

interface GeneralSectionProps {
  addLog: (message: string, tag?: string) => void;
}

export function GeneralSection({ addLog }: GeneralSectionProps) {
  const { t, locale, setLocale } = useLanguage();
  const [s, setS] = useState<Settings>(EMPTY);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [soundOn, setSoundOn] = useState(isUiSoundEnabled());
  // Downloaded archives are kept so reinstalling a build does not fetch tens of
  // gigabytes again. That is worth having, and worth being able to reclaim.
  const [cache, setCache] = useState<{
    total_bytes: number; count: number; in_use_bytes: number; path: string;
    entries: { name: string; bytes: number; in_use: boolean }[];
  } | null>(null);
  const [clearing, setClearing] = useState(false);
  const [confirmClear, setConfirmClear] = useState<"unused" | "all" | null>(null);

  const loadCache = () => {
    api.cacheStats("KOTOR1").then(setCache).catch(() => setCache(null));
  };
  useEffect(loadCache, []);

  const gb = (n: number) => `${(n / 1024 ** 3).toFixed(2)} GB`;

  const openCache = async (name = "") => {
    try {
      await api.openCache("KOTOR1", name);
    } catch {
      addLog("Could not open the downloads folder.", "warning");
    }
  };

  const doClear = async (keepInstalled: boolean) => {
    setClearing(true);
    try {
      const r = await api.clearCache("KOTOR1", { keep_installed: keepInstalled });
      addLog(
        `Removed ${r.removed.length} cached download(s), freeing ${gb(r.freed_bytes)}.`,
        r.removed.length ? "success" : "muted");
      r.failed.forEach(f => addLog(`Could not remove ${f.name}: ${f.reason}`, "warning"));
      loadCache();
    } catch (e: any) {
      addLog(`Could not clear the cache: ${e?.message ?? "error"}`, "error");
    } finally {
      setClearing(false);
      setConfirmClear(null);
    }
  };

  const toggleSound = (on: boolean) => {
    setSoundOn(on);
    setUiSoundEnabled(on);
    if (on) playClick(); // let the user hear the click they just enabled
  };

  useEffect(() => {
    api.getSettings().then(setS).catch(() => {});
  }, []);

  const browse = async () => {
    const dir = await pickDirectory(t("dialog.selectDownloadFolder"));
    if (dir) { setSaved(false); setS((prev) => ({ ...prev, download_dir: dir })); }
  };

  const save = async () => {
    setSaving(true);
    setSaved(false);
    try {
      // Other tabs may have saved credentials or a patcher path since we opened.
      const latest = await api.getSettings();
      await api.setSettings({
        ...latest,
        download_dir: s.download_dir,
        language: locale,
        browser_download_mode: s.browser_download_mode ?? true,
      });
      setSaved(true);
      addLog("Settings saved.", "success");
    } catch (e: any) {
      addLog(`Failed to save settings: ${e?.message}`, "error");
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <Card>
        <CardHeader><CardTitle>{t("settings.general.language")}</CardTitle></CardHeader>
        <CardContent className="space-y-3">
          <div className="space-y-1.5">
            
            <Select
              id="display-language"
              value={locale}
              onChange={(e) => setLocale(e.target.value as Locale)}
              className="max-w-[16rem]"
            >
              {LOCALES.map((l) => (
                <option key={l.value} value={l.value}>{l.label}</option>
              ))}
            </Select>
            <p className="text-xs text-muted-foreground">{t("settings.general.languageHint")}</p>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle>{t("settings.general.sound")}</CardTitle></CardHeader>
        <CardContent>
          <div className="flex items-center justify-between gap-4">
            <div className="space-y-0.5">
              <Label htmlFor="uisound">{t("settings.general.uiSounds")}</Label>
              <p className="text-xs text-muted-foreground">{t("settings.general.uiSoundsHint")}</p>
            </div>
            <Switch id="uisound" checked={soundOn} onCheckedChange={toggleSound} />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle>{t("settings.general.downloads")}</CardTitle></CardHeader>
        <CardContent className="space-y-3">
          <div className="space-y-1.5">
            <Label htmlFor="download-folder">{t("settings.general.downloadFolder")}</Label>
            <div className="flex gap-2">
              <Input
                id="download-folder"
                value={s.download_dir ?? ""}
                onChange={(e) => { setSaved(false); setS({ ...s, download_dir: e.target.value }); }}
              />
              <Button variant="outline" size="icon" onClick={browse} title={t("common.browse")}>
                <FolderOpen />
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              {t("settings.general.downloadFolderHint")}
            </p>
          </div>
          <div className="space-y-1.5">
            <Label>{t("settings.general.screenSize")}</Label>
            <Input
              value={s.preferred_resolution ?? ""}
              placeholder="1920x1080"
              onChange={(e) => { setSaved(false); setS({ ...s, preferred_resolution: e.target.value.trim() }); }}
            />
            <p className="text-xs text-muted-foreground">{t("settings.general.screenSizeHint")}</p>
          </div>
          <div className="flex items-center justify-between gap-4 pt-2 border-t">
            <div className="space-y-0.5">
              <Label htmlFor="browser-dl">Download via web browser</Label>
              <p className="text-xs text-muted-foreground">
                Opens mod pages in your browser (Firefox, Edge, etc.) and watches your Downloads folder. Avoids login and API permission issues with DeadlyStream and Nexus Mods.
              </p>
            </div>
            <Switch
              id="browser-dl"
              checked={s.browser_download_mode ?? true}
              onCheckedChange={(checked) => {
                setSaved(false);
                setS({ ...s, browser_download_mode: checked });
              }}
            />
          </div>
          <div className="flex items-center gap-3">
            <Button onClick={save} disabled={saving}>
              {saving ? t("common.saving") : t("common.save")}
            </Button>
            {saved && <span className="text-xs text-[hsl(var(--success))]">{t("common.saved")}</span>}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Downloaded mods</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-xs text-muted-foreground">
            Downloaded archives are kept for reinstalls. Clear unused downloads
            to free disk space.
          </p>

          {cache === null ? (
            <p className="text-xs text-muted-foreground">Checking…</p>
          ) : cache.count === 0 ? (
            <div className="space-y-2">
              <p className="text-sm text-muted-foreground">Nothing is downloaded yet.</p>
              <Button size="sm" variant="outline" onClick={() => openCache()}>
                <FolderOpen className="mr-1.5 size-3.5" />
                Open folder
              </Button>
              <p className="text-xs text-muted-foreground">
                For manual downloads, use the folder shown in the mod’s error
                message. Place the archive there, then retry the installation.
              </p>
            </div>
          ) : (
            <>
              <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1">
                <span className="text-2xl font-semibold tabular-nums">
                  {gb(cache.total_bytes)}
                </span>
                <span className="text-xs text-muted-foreground">
                  {cache.count} mod{cache.count === 1 ? "" : "s"} cached
                  {cache.in_use_bytes > 0 &&
                    ` · ${gb(cache.in_use_bytes)} belongs to mods you have installed`}
                </span>
              </div>

              <ul className="space-y-1">
                {cache.entries.slice(0, 5).map(e => (
                  <li key={e.name} className="flex items-center gap-2 text-xs">
                    <span className="w-20 shrink-0 text-right tabular-nums text-muted-foreground">
                      {gb(e.bytes)}
                    </span>
                    <button
                      type="button"
                      onClick={() => openCache(e.name)}
                      className="min-w-0 flex-1 truncate text-left hover:underline"
                      title={`Open ${e.name}`}
                    >
                      {e.name}
                    </button>
                    {e.in_use && (
                      <span className="shrink-0 text-[10px] text-muted-foreground">
                        installed
                      </span>
                    )}
                  </li>
                ))}
              </ul>

              <div className="flex flex-wrap items-center gap-2">
                {confirmClear ? (
                  <>
                    <span className="text-xs text-[hsl(var(--destructive))]">
                      {confirmClear === "all"
                        ? `Delete all ${gb(cache.total_bytes)}? Installing again will re-download.`
                        : `Delete cached downloads for mods you have not installed?`}
                    </span>
                    <Button size="sm" variant="destructive" disabled={clearing}
                      onClick={() => doClear(confirmClear === "unused")}>
                      Yes, delete
                    </Button>
                    <Button size="sm" variant="ghost" disabled={clearing}
                      onClick={() => setConfirmClear(null)}>
                      Cancel
                    </Button>
                  </>
                ) : (
                  <>
                    <Button size="sm" variant="outline" onClick={() => openCache()}>
                      <FolderOpen className="mr-1.5 size-3.5" />
                      Open folder
                    </Button>
                    <Button size="sm" variant="outline" disabled={clearing}
                      onClick={() => setConfirmClear("unused")}>
                      Clear unused
                    </Button>
                    <Button size="sm" variant="outline" disabled={clearing}
                      onClick={() => setConfirmClear("all")}>
                      Clear everything
                    </Button>
                  </>
                )}
              </div>
            </>
          )}
        </CardContent>
      </Card>
    </>
  );
}
