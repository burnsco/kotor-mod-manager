import { useEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import {
  Play, Pause, Square, AlertTriangle, RotateCcw, Download, X, FolderInput, UploadCloud,
  FolderOpen, ScrollText, Plus, Trash2,
  Search,
} from "lucide-react";
import { api, type BuildInfo, type BuildMod } from "@/lib/api";
import { pickDirectory, onFilesDropped, onDragHover } from "@/lib/tauri";
import { ModList, type ModRuntime } from "@/components/ModList";
import { ContextMenu, type ContextMenuItem } from "@/components/ui/context-menu";
import { BuildModDetail } from "@/components/BuildModDetail";
import { AddBuildDialog } from "@/components/AddBuildDialog";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { Progress } from "@/components/ui/progress";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { useConfirm } from "@/components/useConfirm";
import { useT } from "@/lib/i18n";

interface BuildsViewProps {
  active: boolean;
  ready: boolean;
  loggedIn: boolean;
  builds: BuildInfo[];
  refreshBuilds: () => Promise<void> | void;
  selectedBuild: string;
  onSelectBuild: (key: string) => void;
  mods: BuildMod[];
  setMods: (mods: BuildMod[]) => void;
  runtime: Record<string, ModRuntime>;
  resetRuntime: () => void;
  activeFileId: string | null;
  running: boolean;
  paused: boolean;
  overall: number;
  done: number;
  errors: number;
  manual: number;
  markManualDone: (fileId: string) => void;
  patcherMod: string | null;
  clearPatcher: () => void;
  addLog: (message: string, tag?: string) => void;
  setRunning: (v: boolean) => void;
  setPaused: (v: boolean) => void;
  requestLogin: () => void;
  activeProfile: string;
}

export function BuildsView(props: BuildsViewProps) {
  const {
    ready, loggedIn, builds, refreshBuilds, selectedBuild, onSelectBuild, mods, setMods, runtime,
    resetRuntime, activeFileId, running, paused, overall, done, errors, manual, markManualDone,
    patcherMod, clearPatcher, addLog, setRunning, setPaused, requestLogin, activeProfile,
  } = props;

  const t = useT();
  const [confirm, confirmDialog] = useConfirm();
  const [loading, setLoading] = useState(false);
  const [openMod, setOpenMod] = useState<BuildMod | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const selectionScope = useRef("");
  const [dragActive, setDragActive] = useState(false);
  const [menu, setMenu] = useState<{ x: number; y: number; mod: BuildMod } | null>(null);
  const [showAddBuild, setShowAddBuild] = useState(false);
  const [installError, setInstallError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [screenResolution, setScreenResolution] = useState("1920x1080");
  const visibleMods = useMemo(() => {
    const term = query.trim().toLowerCase();
    return term ? mods.filter(m => m.name.toLowerCase().includes(term)) : mods;
  }, [mods, query]);

  const selectedBuildInfo = builds.find((b) => b.key === selectedBuild);
  const buildGame = selectedBuildInfo?.game ?? mods[0]?.game ?? "";

  const onBuildAdded = async (build: BuildInfo) => {
    await refreshBuilds();
    onSelectBuild(build.key);
  };

  const deleteSelectedBuild = async () => {
    if (!selectedBuildInfo?.custom) return;
    if (!(await confirm({ message: t("builds.deleteBuildConfirm", { label: selectedBuildInfo.label }), confirmLabel: t("common.delete") }))) return;
    try {
      await api.deleteBuild(selectedBuildInfo.key);
      addLog(t("builds.deleteBuildDone", { label: selectedBuildInfo.label }), "success");
      const remaining = builds.filter((b) => b.key !== selectedBuildInfo.key);
      await refreshBuilds();
      if (remaining[0]) onSelectBuild(remaining[0].key);
    } catch (e: any) {
      addLog(t("builds.deleteBuildFailed", { error: e?.message ?? "error" }), "error");
    }
  };

  const labelFor = (key: string) => builds.find((b) => b.key === key)?.label ?? key;
  const patcherName = patcherMod ? mods.find((m) => m.file_id === patcherMod)?.name : null;

  useEffect(() => {
    api.getSettings().then(s => {
      setScreenResolution(s.preferred_resolution || s.screen_resolution || "1920x1080");
    }).catch(() => {});
  }, []);

  const chooseScreenResolution = async (resolution: string) => {
    if (running || loading) return;
    try {
      const settings = await api.getSettings();
      const { screen_resolution: _detected, ...savedSettings } = settings;
      await api.setSettings({ ...savedSettings, preferred_resolution: resolution });
      setScreenResolution(resolution);
      addLog(`Screen size set to ${resolution.replace("x", " × ")}. Resolution-specific cached mods will be checked, and mismatched installs selected again.`, "success");
      if (mods.length) await loadBuild(false);
    } catch (e: any) {
      addLog(`Could not save screen size: ${e?.message ?? "error"}`, "error");
    }
  };

  // Select all mods by default, but skip already-installed ones.
  useEffect(() => {
    const scope = `${selectedBuild}:${mods.map(m => `${m.file_id}:${!!m.installed}`).join(",")}`;
    if (selectionScope.current === scope) return;
    selectionScope.current = scope;
    setSelected(new Set(mods.filter(m => !m.installed).map((m) => m.file_id)));
  }, [mods, selectedBuild]);

  const toggleMod = (fileId: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(fileId)) next.delete(fileId);
      else next.add(fileId);
      return next;
    });
  };
  const selectAll = () => setSelected(new Set(mods.filter(m => !m.installed).map((m) => m.file_id)));
  const selectNone = () => setSelected(new Set());

  const selectedCount = useMemo(
    () => mods.filter((m) => selected.has(m.file_id)).length,
    [mods, selected],
  );

  // Import a dropped folder of mods, or a single archive.
  const importFolder = async (path: string) => {
    if (!buildGame) { addLog(t("builds.importNoGame"), "warning"); return; }
    try {
      addLog(t("builds.importingFolder", { path }), "info");
      await api.importFolder({ game: buildGame, path, profile: activeProfile || undefined });
    } catch (e: any) {
      addLog(t("builds.importFailed", { error: e?.message ?? "error" }), "error");
    }
  };
  const importArchive = async (path: string) => {
    if (!buildGame) { addLog(t("builds.importNoGame"), "warning"); return; }
    try {
      addLog(t("builds.importingMod", { path }), "info");
      await api.importMod({ game: buildGame, path, profile: activeProfile || undefined });
    } catch (e: any) {
      addLog(t("builds.importFailed", { error: e?.message ?? "error" }), "error");
    }
  };

  const pickImportFolder = async () => {
    const dir = await pickDirectory(t("dialog.selectImportFolder"));
    if (dir) importFolder(dir);
  };

  // Register OS drag-drop listeners (Tauri only; no-op in a browser).
  useEffect(() => {
    if (!props.active) { setDragActive(false); return; }
    let disposed = false;
    const cleanups: Array<() => void> = [];
    onFilesDropped((paths) => {
      setDragActive(false);
      for (const p of paths) {
        const low = p.toLowerCase();
        if (low.endsWith(".zip") || low.endsWith(".7z") || low.endsWith(".rar")) importArchive(p);
        else importFolder(p);
      }
    }).then((un) => { if (disposed) un(); else cleanups.push(un); });
    onDragHover((active) => setDragActive(active))
      .then((un) => { if (disposed) un(); else cleanups.push(un); });
    return () => { disposed = true; cleanups.forEach((c) => c()); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [buildGame, activeProfile, props.active]);

  // Show the saved copy of a build the moment it is selected, so switching
  // builds is instant. The Load button re-fetches the guide for the latest.
  useEffect(() => {
    if (!selectedBuild) return;
    let cancelled = false;
    api.loadBuild(selectedBuild, activeProfile || undefined, false)
      .then(r => { if (!cancelled) setMods(r.mods); })
      .catch(() => {});
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedBuild, activeProfile]);

  const loadBuild = async (refresh = true) => {
    setLoading(true);
    resetRuntime();
    try {
      const r = await api.loadBuild(selectedBuild, activeProfile || undefined, refresh);
      setMods(r.mods);
      const alreadyInstalled = r.mods.filter(m => m.installed).length;
      const where = r.from_cache ? " from your saved copy" : "";
      const msg = alreadyInstalled > 0
        ? `Loaded ${r.mods.length} mods for ${labelFor(selectedBuild)}${where} (${alreadyInstalled} already installed, deselected).`
        : `Loaded ${r.mods.length} mods for ${labelFor(selectedBuild)}${where}.`;
      addLog(msg, "success");
    } catch (e: any) {
      addLog(`Load failed: ${e?.message}`, "error");
    } finally {
      setLoading(false);
    }
  };

  const startInstall = async () => {
    if (mods.length === 0) { addLog("Load a mod list first.", "warning"); return; }
    if (selectedCount === 0) { addLog(t("builds.selectNoneWarn"), "warning"); return; }
    setInstallError(null);
    const fileIds = Array.from(selected);
    try {
      await api.startInstall(selectedBuild, undefined, fileIds);
      setRunning(true);
    } catch (e: any) {
      if (e?.status === 401 || e?.data?.error === "Not logged in") {
        requestLogin();
        return;
      }
      const err = e?.data?.error;
      if (err === "game_path_required" || err === "game_path_invalid") {
        if (err === "game_path_invalid") {
          addLog(t("builds.invalidGameFolder", { game: e.data.game, path: e.data.path ?? "" }), "warning");
        } else {
          addLog(t("builds.pickGameFolder", { game: e.data.game }), "warning");
        }
        const dir = await pickDirectory(t("builds.pickGameFolder", { game: e.data.game }));
        if (!dir) return;
        try {
          await api.startInstall(selectedBuild, dir, fileIds);
          setRunning(true);
        } catch (e2: any) {
          const msg = e2?.data?.error === "game_path_invalid"
            ? t("builds.invalidGameFolder", { game: e2.data.game, path: e2.data.path ?? "" })
            : `Start failed: ${e2?.message}`;
          addLog(msg, "error");
          setInstallError(msg);
        }
      } else {
        addLog(`Start failed: ${e?.message}`, "error");
        setInstallError(`Start failed: ${e?.message}`);
      }
    }
  };

  const control = async (action: "pause" | "resume" | "stop" | "retry") => {
    try {
      await api.control(action);
      if (action === "pause") setPaused(true);
      if (action === "resume") setPaused(false);
      if (action === "stop") { setRunning(false); setPaused(false); }
    } catch (e: any) {
      addLog(`${action} failed: ${e?.message}`, "error");
    }
  };

  const openDownloadFolder = async (mod: BuildMod) => {
    try {
      const r = await api.openDownloadFolder(mod.file_id, mod.slug, mod.game);
      if (r.fallback) addLog(t("builds.downloadFolderMissing", { name: mod.name }), "info");
    } catch {
      addLog(t("builds.downloadFolderMissing", { name: mod.name }), "warning");
    }
  };

  // Open the extracted folder for a mod the player must install by hand.
  const openManualFolder = async (mod: BuildMod) => {
    const folder = runtime[mod.file_id]?.manualFolder;
    try {
      if (folder) await api.openPath(folder);
      else await api.openDownloadFolder(mod.file_id, mod.slug, mod.game);
    } catch {
      addLog(t("builds.downloadFolderMissing", { name: mod.name }), "warning");
    }
  };

  const markDone = (mod: BuildMod) => {
    markManualDone(mod.file_id);
    addLog(t("builds.manualMarkedDone", { name: mod.name }), "success");
  };

  const menuItems = (mod: BuildMod): ContextMenuItem[] => [
    { label: t("modDetail.viewDetails"), icon: ScrollText, onSelect: () => setOpenMod(mod) },
    { label: t("builds.openDownloadFolder"), icon: FolderOpen, onSelect: () => openDownloadFolder(mod) },
  ];

  const showBanner = !!patcherName;

  return (
    <div className="flex h-full flex-col">
      {confirmDialog}
      {/* Header */}
      <header className="view-header flex flex-wrap items-center gap-3 border-b">
        <div>
          <h1 className="text-base font-semibold">{t("builds.title")}</h1>
          <p className="text-xs text-muted-foreground">
            {mods.length > 0
              ? errors
                ? t("builds.summaryErrors", { count: mods.length, done, total: mods.length, errors })
                : manual
                  ? t("builds.summaryManual", { count: mods.length, done, total: mods.length, manual })
                  : t("builds.summary", { count: mods.length, done, total: mods.length })
              : t("builds.subtitle")}
          </p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <span className="text-xs text-muted-foreground">{t("builds.overall")}</span>
          <Progress value={overall} className="w-40" />
          <span className="w-10 text-right font-mono text-xs text-muted-foreground">
            {Math.round(overall)}%
          </span>
        </div>
      </header>

      {/* TSLPatcher banner */}
      {showBanner && (
        <div className="flex items-center gap-2 border-b border-[hsl(var(--warning)/0.4)] bg-[hsl(var(--warning)/0.1)] px-5 py-2.5 text-sm text-[hsl(var(--warning))] animate-fade-in">
          <AlertTriangle className="size-4 shrink-0" />
          <span className="flex-1">{t("builds.patcherBanner", { mod: patcherName ?? "" })}</span>
          <button
            onClick={clearPatcher}
            className="rounded-sm text-[hsl(var(--warning))]/80 transition-colors hover:text-[hsl(var(--warning))]"
            title={t("common.dismiss")}
          >
            <X className="size-4" />
          </button>
        </div>
      )}

      {/* Build selector */}
      <div className="flex flex-wrap items-center gap-2 border-b px-6 py-3">
        <Select
          value={selectedBuild}
          onChange={(e) => onSelectBuild(e.target.value)}
          disabled={running}
          className="w-64"
          aria-label={t("builds.title")}
        >
          {builds.map((b) => (
            <option key={b.key} value={b.key}>{b.label}</option>
          ))}
        </Select>
        <Button
          variant="secondary"
          size="sm"
          onClick={() => loadBuild(true)}
          disabled={loading || running}
          title={t("builds.refreshHint")}
        >
          <Download /> {loading ? t("builds.loading") : t("builds.loadList")}
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={pickImportFolder}
          disabled={running}
          title={t("builds.importFolderHint")}
        >
          <FolderInput /> {t("builds.importFolder")}
        </Button>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setShowAddBuild(true)}
          disabled={running}
          title={t("builds.addBuildHint")}
        >
          <Plus /> {t("builds.addBuild")}
        </Button>
        {selectedBuildInfo?.custom && !running && (
          <Button
            variant="ghost"
            size="icon"
            onClick={deleteSelectedBuild}
            title={t("builds.deleteBuild")}
            className="text-muted-foreground hover:text-destructive"
          >
            <Trash2 />
          </Button>
        )}
        <div className="ml-auto flex items-center gap-1.5" role="group" aria-label={t("builds.screenSize")}>
          <span className="mr-1 text-xs text-muted-foreground">{t("builds.screenSize")}</span>
          {([[
            "1920x1080", "1080p"], ["2560x1440", "2K"], ["3840x2160", "4K"]] as const).map(([resolution, label]) => (
            <Button
              key={resolution}
              variant={screenResolution === resolution ? "secondary" : "outline"}
              size="sm"
              className="h-7 px-2.5 text-xs"
              onClick={() => chooseScreenResolution(resolution)}
              disabled={running || loading}
              aria-pressed={screenResolution === resolution}
              title={`${label} (${resolution.replace("x", " × ")})`}
            >{label}</Button>
          ))}
        </div>
      </div>

      {mods.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 border-b px-6 py-2">
          <div className="relative mr-auto w-64 max-w-full">
            <Search className="pointer-events-none absolute left-2.5 top-2.5 size-4 text-muted-foreground" />
            <Input value={query} onChange={e => setQuery(e.target.value)}
              placeholder={t("library.search")} aria-label={t("library.search")}
              className="pl-9" />
          </div>
          <span className="text-xs text-muted-foreground">
            {t("builds.selectedCount", { selected: selectedCount, total: mods.length })}
          </span>
          {!running && <>
            <Button variant="ghost" size="sm" onClick={selectAll}>{t("builds.selectAll")}</Button>
            <Button variant="ghost" size="sm" onClick={selectNone}>{t("builds.selectNone")}</Button>
          </>}
        </div>
      )}

      {/* Drop zone hint */}
      {dragActive && <div
        className={cn(
          "mx-5 mt-3 flex items-center justify-center gap-2 rounded-lg border border-dashed px-4 py-2.5 text-xs transition-colors",
          dragActive
            ? "border-primary bg-primary/10 text-primary"
            : "border-border/60 text-muted-foreground"
        )}
      >
        <UploadCloud className="size-4 shrink-0" />
        <span>{t("builds.dropHint")}</span>
      </div>}

      {/* Mod list */}
      <div className="min-h-0 flex-1 px-3 py-2">
        <div className="flex h-full flex-col">
          {mods.length > 0 && visibleMods.length === 0 ? (
            <div className="flex h-full flex-col items-center justify-center gap-3 text-sm text-muted-foreground">
              <p>{t("library.noMatchTitle")}</p>
              <Button variant="outline" size="sm" onClick={() => setQuery("")}>{t("common.clear")}</Button>
            </div>
          ) : (
          <ModList
            mods={visibleMods}
            runtime={runtime}
            activeFileId={activeFileId}
            onOpenMod={setOpenMod}
            onContextMenu={(e: MouseEvent, mod) => { e.preventDefault(); setMenu({ x: e.clientX, y: e.clientY, mod }); }}
            selectable={!running}
            selected={selected}
            onToggle={toggleMod}
            onManualOpen={openManualFolder}
            onManualDone={markDone}
          />
          )}
        </div>
      </div>

      {/* Sticky action bar */}
      <footer className="flex flex-wrap items-center gap-3 border-t bg-card px-6 py-3">
        {!running ? (
          <Button onClick={startInstall} disabled={!ready || mods.length === 0 || selectedCount === 0}>
            <Play /> {t("builds.installSelected", { count: selectedCount })}
          </Button>
        ) : (
          <>
            {!paused ? (
              <Button variant="secondary" onClick={() => control("pause")}>
                <Pause /> {t("builds.pause")}
              </Button>
            ) : (
              <Button variant="secondary" onClick={() => control("resume")}>
                <Play /> {t("builds.resume")}
              </Button>
            )}
            <Button variant="destructive" onClick={() => control("stop")}>
              <Square /> {t("builds.stop")}
            </Button>
          </>
        )}
        {!running && <span className="hidden text-xs text-muted-foreground xl:block">{t("builds.dropHint")}</span>}
        {!running && errors > 0 && (
          <Button variant="outline" onClick={() => control("retry").then(startInstall)}>
            <RotateCcw /> {t("builds.retry")}
          </Button>
        )}
        {installError && !running ? (
          <span className="ml-auto flex max-w-sm items-center gap-1.5 text-xs text-destructive">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
            {installError}
          </span>
        ) : (
          <span className={cn("ml-auto text-xs", ready ? "text-muted-foreground" : "text-destructive")}>
            {ready
              ? running
                ? paused ? t("builds.statusPaused") : t("builds.statusInstalling")
                : t("builds.statusIdle")
              : t("builds.statusConnecting")}
          </span>
        )}
      </footer>

      {menu && (
        <ContextMenu
          x={menu.x}
          y={menu.y}
          items={menuItems(menu.mod)}
          onClose={() => setMenu(null)}
        />
      )}

      {openMod && (
        <BuildModDetail
          mod={openMod}
          error={runtime[openMod.file_id]?.error}
          onClose={() => setOpenMod(null)}
        />
      )}

      <AddBuildDialog
        open={showAddBuild}
        onClose={() => setShowAddBuild(false)}
        onAdded={onBuildAdded}
        addLog={addLog}
      />
    </div>
  );
}
