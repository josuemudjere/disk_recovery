#!/usr/bin/env python3
"""
Interface graphique de réparation de clé USB sans perte de données (macOS).

Lancement :  python3 gui.py      (ou double-clic sur Lancer.command)
"""

import datetime
import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import app
from gui_recuperation import FenetreRecuperation

DOSSIERS_SYSTEME = (".Spotlight-V100", ".fseventsd", ".Trashes")


class Fenetre(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Réparation de clé USB")
        self.geometry("920x720")
        self.minsize(780, 600)

        self.file = queue.Queue()          # messages du thread de travail vers l'interface
        self.annuler = threading.Event()
        self.occupe = False
        self.disques = {}
        self.etat_affiche = None
        self.sauvegardes = set()           # disques sauvegardés pendant cette session
        self.boutons = []

        self._construire()
        self.rafraichir()
        self.after(100, self._traiter_file)
        self.after(4000, self._auto_rafraichir)

    # ------------------------------------------------------------------ #
    # Construction de la fenêtre
    # ------------------------------------------------------------------ #

    def _construire(self):
        racine = ttk.Frame(self, padding=14)
        racine.pack(fill="both", expand=True)

        ttk.Label(racine, text="Réparation de clé USB", font=("Helvetica", 20, "bold")).pack(anchor="w")
        ttk.Label(racine, text="Règle d'or : sauvegarder d'abord, réparer ensuite. "
                               "Ce programme ne formate jamais votre clé.",
                  foreground="#555").pack(anchor="w", pady=(0, 8))


        # --- Choix du disque ---
        cadre_disques = ttk.LabelFrame(racine, text=" 1. Choisissez votre clé ", padding=8)
        cadre_disques.pack(fill="x")
        colonnes = ("fs", "taille", "montage")
        self.arbre = ttk.Treeview(cadre_disques, columns=colonnes, height=5, selectmode="browse")
        self.arbre.heading("#0", text="Disque / volume")
        self.arbre.heading("fs", text="Système de fichiers")
        self.arbre.heading("taille", text="Taille")
        self.arbre.heading("montage", text="Monté sur")
        self.arbre.column("#0", width=300)
        self.arbre.column("fs", width=160)
        self.arbre.column("taille", width=90, anchor="e")
        self.arbre.column("montage", width=220)
        self.arbre.pack(side="left", fill="x", expand=True)
        cote = ttk.Frame(cadre_disques)
        cote.pack(side="left", fill="y", padx=(8, 0))
        self._bouton(cote, "Rafraîchir", self.rafraichir).pack(fill="x")
        self.label_vide = ttk.Label(cadre_disques, text="", foreground="#888")

        # --- Actions ---
        cadre_actions = ttk.LabelFrame(racine, text=" 2. Actions (dans l'ordre conseillé) ", padding=8)
        cadre_actions.pack(fill="x", pady=8)
        etapes = [
            ("① Diagnostiquer", self.diagnostiquer, "Vérifie la clé sans rien modifier."),
            ("② Créer une image complète", self.creer_image,
             "Copie toute la clé dans un fichier .img sur votre Mac (recommandé avant de réparer)."),
            ("③ Copier les fichiers", self.copier_fichiers,
             "Copie les fichiers encore lisibles vers un dossier (plus rapide si la clé s'ouvre)."),
            ("④ Réparer la clé", self.reparer, "Corrige le système de fichiers avec l'outil de macOS."),
            ("⑤ Récupérer des fichiers", self.recuperer,
             "Recherche les fichiers présents, supprimés ou perdus et les copie sur le Mac."),
        ]
        for ligne, (texte, action, aide) in enumerate(etapes):
            self._bouton(cadre_actions, texte, action, largeur=26).grid(row=ligne, column=0, sticky="w", pady=2)
            ttk.Label(cadre_actions, text=aide, foreground="#555").grid(row=ligne, column=1, sticky="w", padx=10)

        autres = ttk.Frame(cadre_actions)
        autres.grid(row=len(etapes), column=0, columnspan=2, sticky="w", pady=(8, 0))
        for texte, action in (("Ouvrir une image de sauvegarde", self.ouvrir_image),
                              ("Remonter la clé", self.remonter),
                              ("Éjecter la clé", self.ejecter),
                              ("Réparer la table des partitions…", self.reparer_partitions)):
            self._bouton(autres, texte, action).pack(side="left", padx=(0, 6))

        # --- Progression ---
        cadre_prog = ttk.Frame(racine)
        cadre_prog.pack(fill="x")
        self.barre = ttk.Progressbar(cadre_prog, mode="determinate", maximum=100)
        self.barre.pack(side="left", fill="x", expand=True)
        self.bouton_annuler = ttk.Button(cadre_prog, text="Annuler", state="disabled",
                                         command=self.annuler.set)
        self.bouton_annuler.pack(side="left", padx=(8, 0))
        self.statut = ttk.Label(racine, text="Prêt.")
        self.statut.pack(anchor="w", pady=(4, 6))

        # --- Journal ---
        cadre_journal = ttk.LabelFrame(racine, text=" Journal ", padding=6)
        cadre_journal.pack(fill="both", expand=True)
        self.journal = tk.Text(cadre_journal, height=12, wrap="word", font=("Menlo", 11),
                               state="disabled", borderwidth=0)
        defil = ttk.Scrollbar(cadre_journal, command=self.journal.yview)
        self.journal.configure(yscrollcommand=defil.set)
        self.journal.pack(side="left", fill="both", expand=True)
        defil.pack(side="right", fill="y")
        self.journal.tag_configure("ok", foreground="#1a7f37")
        self.journal.tag_configure("erreur", foreground="#c62828")
        self.journal.tag_configure("titre", font=("Menlo", 11, "bold"))

    def _bouton(self, parent, texte, commande, largeur=None):
        b = ttk.Button(parent, text=texte, command=commande, width=largeur)
        self.boutons.append(b)
        return b

    # ------------------------------------------------------------------ #
    # Communication thread de travail → interface
    # ------------------------------------------------------------------ #

    def log(self, texte, tag=None):
        self.file.put(("log", texte, tag))

    def _traiter_file(self):
        try:
            while True:
                msg = self.file.get_nowait()
                genre = msg[0]
                if genre == "log":
                    self.journal.configure(state="normal")
                    self.journal.insert("end", msg[1] + "\n", msg[2] or ())
                    self.journal.see("end")
                    self.journal.configure(state="disabled")
                elif genre == "progres":
                    if self.barre["mode"] != "determinate":
                        self.barre.stop()
                        self.barre.configure(mode="determinate")
                    self.barre["value"] = msg[1]
                elif genre == "statut":
                    self.statut.configure(text=msg[1])
                elif genre == "info":
                    messagebox.showinfo(msg[1], msg[2], parent=self)
                elif genre == "alerte":
                    messagebox.showwarning(msg[1], msg[2], parent=self)
                elif genre == "fini":
                    self._fin_operation()
        except queue.Empty:
            pass
        self.after(100, self._traiter_file)

    def lancer(self, titre, fonction, annulable=False):
        """Exécute `fonction` dans un thread pour ne pas figer la fenêtre."""
        if self.occupe:
            return
        self.occupe = True
        self.annuler.clear()
        for b in self.boutons:
            b.state(["disabled"])
        if annulable:
            self.bouton_annuler.state(["!disabled"])
            self.barre.configure(mode="determinate", value=0)
        else:
            self.barre.configure(mode="indeterminate")
            self.barre.start(12)
        self.statut.configure(text=titre + "…")
        self.log(f"\n=== {titre} — {datetime.datetime.now():%H:%M:%S} ===", "titre")

        def travail():
            try:
                fonction()
            except Exception as e:  # noqa: BLE001 — on affiche toute erreur à l'utilisateur
                self.log(f"Erreur : {e}", "erreur")
                self.file.put(("alerte", "Erreur", str(e)))
            finally:
                self.file.put(("fini",))

        threading.Thread(target=travail, daemon=True).start()

    def _fin_operation(self):
        self.occupe = False
        self.barre.stop()
        self.barre.configure(mode="determinate", value=0)
        self.bouton_annuler.state(["disabled"])
        for b in self.boutons:
            b.state(["!disabled"])
        if self.statut.cget("text").endswith("…"):
            self.statut.configure(text="Prêt.")
        self.rafraichir()

    def commande(self, args, entree=None):
        """Lance une commande et recopie sa sortie dans le journal. Renvoie True si succès."""
        self.log("$ " + " ".join(args))
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             stdin=subprocess.PIPE if entree else subprocess.DEVNULL)
        if entree:
            p.stdin.write(entree)
            p.stdin.close()
        for ligne in p.stdout:
            self.log(ligne.rstrip())
        ok = p.wait() == 0
        self.log("→ terminé" if ok else f"→ échec (code {p.returncode})", "ok" if ok else "erreur")
        return ok

    # ------------------------------------------------------------------ #
    # Liste des disques
    # ------------------------------------------------------------------ #

    def rafraichir(self):
        try:
            disques = app.lister_disques_externes()
        except RuntimeError as e:
            self.log(f"Erreur de détection : {e}", "erreur")
            disques = []
        self.disques = {d["id"]: d for d in disques}

        etat = repr(disques)
        if etat == self.etat_affiche:
            return
        self.etat_affiche = etat
        selection = self.arbre.selection()
        self.arbre.delete(*self.arbre.get_children())
        for d in disques:
            self.arbre.insert("", "end", iid=d["id"], open=True,
                              text=f"{d['nom']}  (/dev/{d['id']})",
                              values=(d["protocole"], app.taille_lisible(d["taille"]), ""))
            for v in d["volumes"]:
                if v is d or v.get("DeviceIdentifier") == d["id"]:
                    continue
                self.arbre.insert(d["id"], "end", iid="vol:" + v["DeviceIdentifier"],
                                  text=f"{v.get('VolumeName') or '(sans nom)'}  ({v['DeviceIdentifier']})",
                                  values=(v.get("Content", "?"), app.taille_lisible(v.get("Size", 0)),
                                          v.get("MountPoint") or "non monté"))
        if not disques:
            self.label_vide.configure(text="Aucune clé détectée — branchez-la, elle apparaîtra automatiquement.")
            self.label_vide.pack(side="bottom", anchor="w")
        else:
            self.label_vide.pack_forget()
            garde = [i for i in selection if self.arbre.exists(i)]
            self.arbre.selection_set(garde or [disques[0]["id"]])

    def _auto_rafraichir(self):
        if not self.occupe:
            self.rafraichir()
        self.after(4000, self._auto_rafraichir)

    def recuperer(self):
        """Ouvre la fenêtre de récupération (fonctionne aussi sans clé, depuis une image)."""
        fen = getattr(self, "fenetre_recuperation", None)
        if fen is not None and fen.winfo_exists():
            fen.lift()
            return
        sel = self.arbre.selection()
        disque = self.disques.get(self.arbre.parent(sel[0]) or sel[0]) if sel else None
        self.fenetre_recuperation = FenetreRecuperation(self, disque)

    def disque_choisi(self):
        sel = self.arbre.selection()
        if not sel:
            messagebox.showinfo("Aucune clé", "Sélectionnez d'abord une clé dans la liste.", parent=self)
            return None
        iid = self.arbre.parent(sel[0]) or sel[0]
        return self.disques.get(iid)

    # ------------------------------------------------------------------ #
    # Actions
    # ------------------------------------------------------------------ #

    def diagnostiquer(self):
        disque = self.disque_choisi()
        if not disque:
            return

        def travail():
            ok = all([self.commande(["diskutil", "verifyVolume", v["DeviceIdentifier"]])
                      for v in disque["volumes"]])
            if ok:
                self.log("Aucun problème détecté.", "ok")
                self.file.put(("info", "Diagnostic", "Aucun problème détecté sur la clé."))
            else:
                self.log("Des erreurs ont été trouvées.", "erreur")
                self.file.put(("alerte", "Diagnostic",
                               "Des erreurs ont été trouvées.\n\nCréez une image complète (②) "
                               "avant de réparer (④)."))
        self.lancer("Diagnostic", travail)

    def creer_image(self):
        disque = self.disque_choisi()
        if not disque:
            return
        taille = disque["taille"]
        nom = f"sauvegarde_{disque['id']}_{datetime.datetime.now():%Y%m%d_%H%M%S}.img"
        destination = filedialog.asksaveasfilename(
            parent=self, title="Où enregistrer l'image de la clé ?", initialfile=nom,
            initialdir=os.path.join(app.dossier_utilisateur(), "Desktop"), defaultextension=".img")
        if not destination:
            return
        destination = os.path.abspath(destination)
        erreur = app.verifier_destination(disque, destination)
        if erreur:
            messagebox.showerror("Destination invalide", erreur, parent=self)
            return
        libre = shutil.disk_usage(os.path.dirname(destination)).free
        if libre < taille:
            if not messagebox.askyesno(
                    "Espace peut-être insuffisant",
                    f"La clé fait {app.taille_lisible(taille)} mais il ne reste que "
                    f"{app.taille_lisible(libre)} libres à cet endroit.\n\n"
                    "Les zones vides de la clé ne prennent pas de place : si la clé n'est pas pleine, "
                    "l'image peut tenir. Sinon, la copie s'arrêtera proprement avant de remplir "
                    "votre disque.\n\nLe plus sûr est d'enregistrer l'image sur un disque externe "
                    "assez grand (exFAT ou APFS).\n\nEssayer quand même ici ?",
                    icon="warning", parent=self):
                return
        elif not messagebox.askokcancel(
                "Créer l'image",
                f"Copie de {app.taille_lisible(taille)} vers :\n{destination}\n\n"
                "La clé sera démontée pendant la copie. Rien ne sera écrit dessus."
                + ("" if os.geteuid() == 0 else "\n\nLe Mac va vous demander votre mot de passe."),
                parent=self):
            return

        # Démontage (rapide) avant de démarrer la copie
        res = subprocess.run(["diskutil", "unmountDisk", disque["id"]], capture_output=True, text=True)
        if res.returncode != 0:
            if not messagebox.askyesno("Démontage impossible",
                                       "La clé est utilisée par une application.\n\n"
                                       "Fermez les fichiers ouverts, ou forcez le démontage. Forcer ?",
                                       parent=self):
                return
            res = subprocess.run(["diskutil", "unmountDisk", "force", disque["id"]],
                                 capture_output=True, text=True)
            if res.returncode != 0:
                messagebox.showerror("Démontage impossible", res.stderr or res.stdout, parent=self)
                return

        def progression(pos, total, vitesse, reste, nb_def):
            self.file.put(("progres", pos * 100 / total))
            self.file.put(("statut", f"Image : {pos * 100 / total:.1f} %  —  {app.taille_lisible(pos)} / "
                                     f"{app.taille_lisible(total)}  —  {app.taille_lisible(vitesse)}/s  —  "
                                     f"reste ~{int(reste // 60)} min {int(reste % 60):02d} s  —  "
                                     f"secteurs illisibles : {nb_def}"))

        def travail():
            source = f"/dev/r{disque['id']}"
            self.log(f"Copie de {source} vers {destination}")
            try:
                self.log("Demande d'autorisation pour lire la clé (mot de passe du Mac)…")
                self.file.put(("statut", "En attente du mot de passe…"))
                fd = app.ouvrir_avec_autorisation(source)
                copie, defectueux, motif = app.copier_peripherique(
                    fd, destination, taille, disque["bloc"],
                    progression=progression, annuler=self.annuler)
                nb_def = len(defectueux)
            finally:
                self.commande(["diskutil", "mountDisk", disque["id"]])

            message = app.message_fin_image(destination, copie, nb_def, motif)
            if motif:
                self.log(message, "erreur")
                self.file.put(("statut", "Image incomplète."))
                self.file.put(("alerte", "Image incomplète", message))
            else:
                self.sauvegardes.add(disque["id"])
                self.log(message, "ok" if not nb_def else "erreur")
                self.file.put(("statut", "Image terminée."))
                self.file.put(("info", "Sauvegarde terminée",
                               message + "\n\nVous pouvez l'ouvrir avec « Ouvrir une image de sauvegarde »."))

        self.lancer("Création de l'image", travail, annulable=True)

    def copier_fichiers(self):
        disque = self.disque_choisi()
        if not disque:
            return
        montes = [v for v in disque["volumes"] if v.get("MountPoint")]
        if not montes:
            messagebox.showinfo("Clé non montée",
                                "Aucun volume de la clé n'est accessible.\n\nEssayez « Remonter la clé », "
                                "sinon utilisez l'image complète (②).", parent=self)
            return
        parent_dest = filedialog.askdirectory(parent=self, title="Où copier les fichiers ?",
                                              initialdir=os.path.join(app.dossier_utilisateur(), "Desktop"))
        if not parent_dest:
            return
        racine = os.path.join(parent_dest, f"fichiers_cle_{datetime.datetime.now():%Y%m%d_%H%M%S}")

        def travail():
            self.file.put(("statut", "Inventaire des fichiers…"))
            a_copier = []
            echecs = []
            for v in montes:
                src = v["MountPoint"]
                dest = os.path.join(racine, v.get("VolumeName") or v["DeviceIdentifier"])
                for dossier, sous, fichiers in os.walk(src, onerror=lambda e: echecs.append(str(e))):
                    sous[:] = [d for d in sous if d not in DOSSIERS_SYSTEME]
                    cible = os.path.join(dest, os.path.relpath(dossier, src))
                    a_copier += [(os.path.join(dossier, f), os.path.join(cible, f)) for f in fichiers]
            total = len(a_copier)
            self.log(f"{total} fichier(s) à copier vers {racine}")

            copies = 0
            for i, (src, dst) in enumerate(a_copier, 1):
                if self.annuler.is_set():
                    self.log("Copie annulée.", "erreur")
                    break
                try:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
                    copies += 1
                except (OSError, shutil.Error) as e:
                    echecs.append(f"{src} : {e}")
                    self.log(f"Illisible : {src}", "erreur")
                if i % 20 == 0 or i == total:
                    self.file.put(("progres", i * 100 / max(total, 1)))
                    self.file.put(("statut", f"Copie : {i} / {total} fichiers — {len(echecs)} échec(s)"))

            if os.path.isdir(racine):
                if echecs:
                    with open(os.path.join(racine, "fichiers_non_copies.txt"), "w") as f:
                        f.write("\n".join(echecs) + "\n")
                for dossier, _, fichiers in os.walk(racine):
                    app.rendre_a_l_utilisateur(dossier)
                    for f in fichiers:
                        app.rendre_a_l_utilisateur(os.path.join(dossier, f))

            if not self.annuler.is_set():
                self.sauvegardes.add(disque["id"])
            msg = f"{copies} fichier(s) copiés dans :\n{racine}"
            if echecs:
                msg += (f"\n\n{len(echecs)} élément(s) illisible(s) (voir fichiers_non_copies.txt). "
                        "Pour ceux-là, essayez l'image complète (②).")
            self.log(f"{copies} fichier(s) copiés.", "ok")
            self.file.put(("statut", "Copie terminée."))
            self.file.put(("info", "Copie terminée", msg))

        self.lancer("Copie des fichiers", travail, annulable=True)

    def reparer(self):
        disque = self.disque_choisi()
        if not disque:
            return
        if disque["id"] not in self.sauvegardes:
            if not messagebox.askyesno(
                    "Pas de sauvegarde",
                    "Vous n'avez pas encore sauvegardé cette clé.\n\n"
                    "La réparation corrige la structure de la clé ; dans de rares cas, des fichiers "
                    "très abîmés peuvent être déplacés ou supprimés.\n\n"
                    "Il est fortement conseillé de créer d'abord une image complète (②).\n\n"
                    "Réparer quand même ?", icon="warning", default="no", parent=self):
                return
        elif not messagebox.askokcancel("Réparer", "Lancer la réparation de la clé ?", parent=self):
            return

        def travail():
            resultats = [self.commande(["diskutil", "repairVolume", v["DeviceIdentifier"]])
                         for v in disque["volumes"]]
            self.commande(["diskutil", "mountDisk", disque["id"]])
            if all(resultats):
                self.file.put(("info", "Réparation terminée", "La clé a été réparée (ou était déjà saine)."))
            else:
                self.file.put(("alerte", "Réparation incomplète",
                               "La réparation a échoué sur au moins un volume (voir le journal).\n\n"
                               "Si vous avez créé une image, vos données restent récupérables avec un "
                               "outil comme PhotoRec (brew install testdisk)."))
        self.lancer("Réparation", travail)

    def reparer_partitions(self):
        disque = self.disque_choisi()
        if not disque:
            return
        avert = ("À utiliser seulement si la clé n'affiche aucune partition ou si macOS la dit "
                 "« non initialisée ».\n\nCette opération réécrit la table des partitions.")
        if disque["id"] not in self.sauvegardes:
            avert += "\n\n⚠ Vous n'avez pas encore créé d'image de cette clé."
        if not messagebox.askyesno("Réparer la table des partitions", avert + "\n\nContinuer ?",
                                   icon="warning", default="no", parent=self):
            return
        self.lancer("Réparation de la table des partitions",
                    lambda: self.commande(["diskutil", "repairDisk", disque["id"]], entree="y\n"))

    def ouvrir_image(self):
        chemin = filedialog.askopenfilename(
            parent=self, title="Choisir une image de sauvegarde",
            initialdir=os.path.join(app.dossier_utilisateur(), "Desktop"),
            filetypes=[("Images disque", "*.img *.dmg *.iso"), ("Tous les fichiers", "*")])
        if not chemin:
            return

        def travail():
            if self.commande(["hdiutil", "attach", "-readonly",
                              "-imagekey", "diskimage-class=CRawDiskImage", chemin]):
                self.file.put(("info", "Image ouverte",
                               "L'image est ouverte en lecture seule : ses volumes apparaissent dans le Finder."))
        self.lancer("Ouverture de l'image", travail)

    def remonter(self):
        disque = self.disque_choisi()
        if disque:
            self.lancer("Montage de la clé", lambda: self.commande(["diskutil", "mountDisk", disque["id"]]))

    def ejecter(self):
        disque = self.disque_choisi()
        if disque:
            self.lancer("Éjection de la clé", lambda: self.commande(["diskutil", "eject", disque["id"]]))


def main():
    if sys.platform != "darwin":
        sys.exit("Ce programme est prévu pour macOS.")
    Fenetre().mainloop()


if __name__ == "__main__":
    main()
