"""Fenêtre « Récupérer des fichiers » : recherche rapide (noms) et approfondie (par type)."""

import datetime
import os
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import app
import recuperation as r

COULEUR_SUPPRIME = "#b35c00"
COULEUR_FAIBLE = "#c62828"


class FenetreRecuperation(tk.Toplevel):
    def __init__(self, parent, disque):
        super().__init__(parent)
        self.title("Récupérer des fichiers")
        self.geometry("1020x760")
        self.minsize(860, 620)
        self.disque_initial = disque
        self.liste_disques = []

        self.file = queue.Queue()
        self.annuler = threading.Event()
        self.occupe = False
        self.boutons = []
        self.lecteur = None
        self.source_ouverte = None
        self.fs = None
        self.elements = []
        self.dernier_dossier = None

        self._construire()
        self.protocol("WM_DELETE_WINDOW", self.fermer)
        self.after(100, self._traiter_file)

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #

    def _construire(self):
        racine = ttk.Frame(self, padding=14)
        racine.pack(fill="both", expand=True)
        ttk.Label(racine, text="Récupérer des fichiers", font=("Helvetica", 18, "bold")).pack(anchor="w")
        ttk.Label(racine, text="Recherche les fichiers présents, supprimés ou perdus et les copie sur votre Mac. "
                               "La clé est seulement lue : rien n'y est écrit.",
                  foreground="#555").pack(anchor="w", pady=(0, 8))

        # --- Source ---
        cadre = ttk.LabelFrame(racine, text=" Où chercher ? ", padding=8)
        cadre.pack(fill="x")
        self.source = tk.StringVar(value="cle")
        ttk.Radiobutton(cadre, text="Un disque branché :", value="cle",
                        variable=self.source).grid(row=0, column=0, sticky="w")
        self.choix_disque = tk.StringVar()
        self.menu_disques = ttk.Combobox(cadre, textvariable=self.choix_disque, state="readonly", width=70)
        self.menu_disques.grid(row=0, column=1, padx=6, sticky="we")
        self.menu_disques.bind("<<ComboboxSelected>>", lambda _: self.source.set("cle"))
        self._bouton(cadre, "Actualiser", self.actualiser_disques).grid(row=0, column=2)
        ttk.Radiobutton(cadre, text="Une image de sauvegarde (.img) :", value="image",
                        variable=self.source).grid(row=1, column=0, sticky="w")
        self.chemin_image = tk.StringVar()
        ttk.Entry(cadre, textvariable=self.chemin_image, width=60, state="readonly").grid(row=1, column=1, padx=6)
        self._bouton(cadre, "Choisir…", self.choisir_image).grid(row=1, column=2)
        ttk.Label(cadre, text="Vérifiez que c'est bien la clé abîmée qui est choisie, et non votre disque dur. "
                              "Si vous avez créé une image (bouton ②), utilisez-la : la clé n'est alors plus "
                              "sollicitée.", foreground="#555", wraplength=900).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.actualiser_disques()

        # --- Onglets ---
        onglets = ttk.Notebook(racine)
        onglets.pack(fill="both", expand=True, pady=8)
        self._onglet_rapide(onglets)
        self._onglet_approfondi(onglets)

        # --- Progression ---
        bas = ttk.Frame(racine)
        bas.pack(fill="x")
        self.barre = ttk.Progressbar(bas, mode="determinate", maximum=100)
        self.barre.pack(side="left", fill="x", expand=True)
        self.bouton_annuler = ttk.Button(bas, text="Annuler", state="disabled", command=self.annuler.set)
        self.bouton_annuler.pack(side="left", padx=(8, 0))
        self.bouton_ouvrir = ttk.Button(bas, text="Ouvrir le dossier récupéré", state="disabled",
                                        command=self.ouvrir_dossier)
        self.bouton_ouvrir.pack(side="left", padx=(8, 0))
        self.statut = ttk.Label(racine, text="Prêt.")
        self.statut.pack(anchor="w", pady=(4, 0))

    def _onglet_rapide(self, onglets):
        page = ttk.Frame(onglets, padding=8)
        onglets.add(page, text="  Recherche rapide (avec les noms)  ")
        ttk.Label(page, text="Relit le système de fichiers de la clé (FAT32), même si macOS ne la reconnaît plus. "
                             "Retrouve les noms, les dossiers et les fichiers supprimés.",
                  foreground="#555", wraplength=900).pack(anchor="w")

        barre = ttk.Frame(page)
        barre.pack(fill="x", pady=6)
        self._bouton(barre, "Analyser", self.analyser).pack(side="left")
        self.voir_presents = tk.BooleanVar(value=True)
        self.voir_supprimes = tk.BooleanVar(value=True)
        ttk.Checkbutton(barre, text="Fichiers présents", variable=self.voir_presents,
                        command=self.afficher).pack(side="left", padx=(16, 0))
        ttk.Checkbutton(barre, text="Fichiers supprimés", variable=self.voir_supprimes,
                        command=self.afficher).pack(side="left", padx=(8, 0))
        ttk.Label(barre, text="Rechercher :").pack(side="left", padx=(16, 4))
        self.recherche = tk.StringVar()
        self.recherche.trace_add("write", lambda *_: self.afficher())
        ttk.Entry(barre, textvariable=self.recherche, width=24).pack(side="left")

        cadre_arbre = ttk.Frame(page)
        cadre_arbre.pack(fill="both", expand=True)
        self.arbre = ttk.Treeview(cadre_arbre, columns=("taille", "etat", "chance", "date"), selectmode="extended")
        for col, titre, largeur, ancre in (("#0", "Nom", 420, "w"), ("taille", "Taille", 90, "e"),
                                           ("etat", "État", 90, "w"), ("chance", "Chance de récupération", 160, "w"),
                                           ("date", "Modifié le", 130, "w")):
            self.arbre.heading(col, text=titre)
            self.arbre.column(col, width=largeur, anchor=ancre)
        self.arbre.tag_configure("supprime", foreground=COULEUR_SUPPRIME)
        self.arbre.tag_configure("faible", foreground=COULEUR_FAIBLE)
        defil = ttk.Scrollbar(cadre_arbre, command=self.arbre.yview)
        self.arbre.configure(yscrollcommand=defil.set)
        self.arbre.pack(side="left", fill="both", expand=True)
        defil.pack(side="right", fill="y")

        actions = ttk.Frame(page)
        actions.pack(fill="x", pady=(6, 0))
        self._bouton(actions, "Récupérer la sélection", self.recuperer_selection).pack(side="left")
        self._bouton(actions, "Récupérer tout ce qui est affiché", self.recuperer_tout).pack(side="left", padx=6)
        self.resume = ttk.Label(actions, text="", foreground="#555")
        self.resume.pack(side="right")
        ttk.Label(page, text="Astuce : sélectionner un dossier récupère tout son contenu ; ⌘+clic pour en choisir "
                             "plusieurs. En orange : fichiers supprimés (la 1re lettre d'un nom court peut être "
                             "remplacée par « _ »).", foreground="#777", wraplength=900).pack(anchor="w", pady=(4, 0))

    def _onglet_approfondi(self, onglets):
        page = ttk.Frame(onglets, padding=8)
        onglets.add(page, text="  Recherche approfondie (par type)  ")
        ttk.Label(page, text="À utiliser si la recherche rapide ne trouve rien ou si des fichiers manquent. "
                             "Parcourt toute la clé et reconnaît les fichiers d'après leur format, même si le système "
                             "de fichiers est détruit.\n\nLes noms et dossiers d'origine sont perdus : les fichiers "
                             "sont renommés et rangés par type. Les gros fichiers très fragmentés peuvent être "
                             "incomplets. Durée : environ le temps de lire toute la clé.",
                  foreground="#555", wraplength=900, justify="left").pack(anchor="w")
        cadre = ttk.LabelFrame(page, text=" Types de fichiers à rechercher ", padding=8)
        cadre.pack(fill="x", pady=10)
        self.categories = {}
        for i, (cat, exts) in enumerate(r.CATEGORIES.items()):
            v = tk.BooleanVar(value=True)
            self.categories[cat] = v
            ttk.Checkbutton(cadre, text=f"{cat}  ({', '.join(exts)})", variable=v).grid(row=i, column=0, sticky="w")
        self._bouton(page, "Lancer la recherche approfondie", self.approfondie).pack(anchor="w")
        self.resultat_approfondi = ttk.Label(page, text="", font=("Helvetica", 13))
        self.resultat_approfondi.pack(anchor="w", pady=10)

    def _bouton(self, parent, texte, commande):
        b = ttk.Button(parent, text=texte, command=commande)
        self.boutons.append(b)
        return b

    # ------------------------------------------------------------------ #
    # Thread de travail
    # ------------------------------------------------------------------ #

    def _traiter_file(self):
        try:
            while True:
                msg = self.file.get_nowait()
                genre = msg[0]
                if genre == "progres":
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
                elif genre == "elements":
                    self.elements = msg[1]
                    self.afficher()
                elif genre == "approfondi":
                    self.resultat_approfondi.configure(text=msg[1])
                elif genre == "dossier":
                    self.dernier_dossier = msg[1]
                    self.bouton_ouvrir.state(["!disabled"])
                elif genre == "fini":
                    self._fin()
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._traiter_file)

    def lancer(self, titre, fonction, determinee=False):
        if self.occupe:
            return
        self.occupe = True
        self.annuler.clear()
        for b in self.boutons:
            b.state(["disabled"])
        self.bouton_annuler.state(["!disabled"])
        if determinee:
            self.barre.configure(mode="determinate", value=0)
        else:
            self.barre.configure(mode="indeterminate")
            self.barre.start(12)
        self.statut.configure(text=titre + "…")

        def travail():
            try:
                fonction()
            except Exception as e:  # noqa: BLE001 — toute erreur est montrée à l'utilisateur
                self.file.put(("statut", f"Erreur : {e}"))
                self.file.put(("alerte", "Erreur", str(e)))
            finally:
                self.file.put(("fini",))

        threading.Thread(target=travail, daemon=True).start()

    def _fin(self):
        self.occupe = False
        self.barre.stop()
        self.barre.configure(mode="determinate", value=0)
        self.bouton_annuler.state(["disabled"])
        for b in self.boutons:
            b.state(["!disabled"])

    def fermer(self):
        if self.occupe:
            messagebox.showinfo("Opération en cours", "Cliquez d'abord sur « Annuler » et attendez la fin.",
                                parent=self)
            return
        if self.lecteur:
            self.lecteur.fermer()
        self.destroy()

    # ------------------------------------------------------------------ #
    # Source
    # ------------------------------------------------------------------ #

    def choisir_image(self):
        chemin = filedialog.askopenfilename(
            parent=self, title="Choisir une image de sauvegarde",
            initialdir=os.path.join(app.dossier_utilisateur(), "Desktop"),
            filetypes=[("Images disque", "*.img *.dmg *.iso *.bin"), ("Tous les fichiers", "*")])
        if chemin:
            self.chemin_image.set(chemin)
            self.source.set("image")

    @staticmethod
    def description_disque(d):
        montes = [v.get("VolumeName") or v["DeviceIdentifier"] for v in d["volumes"] if v.get("MountPoint")]
        etat = f"volumes : {', '.join(montes)}" if montes else "non lisible par macOS"
        return f"{d['nom']} — {app.taille_lisible(d['taille'])} — /dev/{d['id']} — {etat}"

    def actualiser_disques(self):
        ancien = self.disque_choisi()
        try:
            self.liste_disques = app.lister_disques_externes()
        except RuntimeError:
            self.liste_disques = []
        self.menu_disques["values"] = [self.description_disque(d) for d in self.liste_disques]
        if not self.liste_disques:
            self.choix_disque.set("Aucun disque externe détecté")
            self.source.set("image")
            return
        # Choix par défaut : le disque déjà choisi, sinon celui sélectionné dans la fenêtre principale,
        # sinon un disque que macOS n'arrive pas à lire (c'est en général la clé abîmée).
        voulu = ancien or self.disque_initial
        ids = [d["id"] for d in self.liste_disques]
        if voulu and voulu["id"] in ids:
            index = ids.index(voulu["id"])
        else:
            illisibles = [i for i, d in enumerate(self.liste_disques)
                          if not any(v.get("MountPoint") for v in d["volumes"])]
            index = illisibles[0] if illisibles else 0
        self.menu_disques.current(index)

    def disque_choisi(self):
        if not self.liste_disques:
            return None
        i = self.menu_disques.current()
        return self.liste_disques[i] if 0 <= i < len(self.liste_disques) else None

    def source_valide(self):
        if self.source.get() == "image" and not self.chemin_image.get():
            messagebox.showinfo("Image", "Choisissez d'abord le fichier image.", parent=self)
            return False
        if self.source.get() == "cle" and not self.disque_choisi():
            messagebox.showinfo("Disque", "Aucun disque choisi. Branchez la clé puis cliquez sur « Actualiser ».",
                                parent=self)
            return False
        return True

    def source_voulue(self):
        """Source choisie, lue dans la fenêtre (à appeler hors du thread de travail)."""
        if self.source.get() == "cle":
            d = self.disque_choisi()
            return ("cle", d["id"], d["taille"], d["bloc"])
        return ("image", self.chemin_image.get())

    def ouvrir_lecteur(self, voulu):
        """Ouvre la source `voulu` (appelé dans le thread de travail)."""
        if self.source_ouverte == voulu and self.lecteur:
            return self.lecteur
        if self.lecteur:
            self.lecteur.fermer()
            self.lecteur, self.source_ouverte, self.fs = None, None, None
        if voulu[0] == "cle":
            _, ident, taille, bloc = voulu
            self.file.put(("statut", "En attente du mot de passe pour lire le disque…"))
            self.lecteur = r.ouvrir_source(f"/dev/r{ident}", taille, bloc)
        else:
            self.lecteur = r.ouvrir_source(voulu[1])
        self.source_ouverte = voulu
        return self.lecteur

    def demander_destination(self, titre):
        dossier = filedialog.askdirectory(parent=self, title=titre,
                                          initialdir=os.path.join(app.dossier_utilisateur(), "Desktop"))
        if not dossier:
            return None
        dossier = os.path.realpath(dossier)
        source = self.disque_choisi() if self.source.get() == "cle" else None
        if source and app.volume_du_disque(dossier, source):
            messagebox.showerror(
                "Destination invalide",
                f"Ce dossier se trouve sur « {source['nom']} » ({app.taille_lisible(source['taille'])}), "
                "c'est-à-dire le disque que vous êtes en train d'analyser.\n\n"
                "On ne récupère jamais des fichiers sur le disque analysé : cela pourrait écraser "
                "ceux qui ne sont pas encore récupérés.\n\n"
                "• Si vous voulez enregistrer sur ce disque, c'est que le mauvais disque est choisi "
                "dans « Où chercher ? » : sélectionnez la clé abîmée, puis relancez l'analyse.\n"
                "• Sinon, choisissez un dossier sur un autre disque.", parent=self)
            return None
        fs = app.type_fs(dossier)
        if fs == "ntfs" or not os.access(dossier, os.W_OK):
            messagebox.showerror(
                "Disque en lecture seule",
                "macOS ne peut pas écrire dans ce dossier."
                + ("\n\nCe disque est au format NTFS (Windows) : le Mac peut le lire mais pas y écrire. "
                   "Choisissez un autre disque ou volume (par exemple en Mac OS étendu, APFS ou exFAT)."
                   if fs == "ntfs" else ""), parent=self)
            return None
        racine = os.path.join(dossier, f"Récupération_{datetime.datetime.now():%Y%m%d_%H%M%S}")
        os.makedirs(racine, exist_ok=True)
        app.rendre_a_l_utilisateur(racine)
        return racine

    # ------------------------------------------------------------------ #
    # Recherche rapide
    # ------------------------------------------------------------------ #

    def analyser(self):
        if not self.source_valide():
            return
        voulu = self.source_voulue()

        def travail():
            lecteur = self.ouvrir_lecteur(voulu)
            self.file.put(("statut", "Recherche du système de fichiers…"))
            fs, message = r.trouver_fat32(lecteur)
            if not fs:
                self.file.put(("elements", []))
                self.file.put(("statut", message))
                self.file.put(("alerte", "Recherche rapide impossible", message))
                return
            self.fs = fs
            elements = fs.analyser(annuler=self.annuler, signaler=lambda n: self.file.put(
                ("statut", f"{message}  Analyse des dossiers… {n} fichier(s) trouvé(s)")))
            self.file.put(("elements", elements))
            supprimes = sum(e.supprime for e in elements)
            texte = (f"{message}  {len(elements) - supprimes} fichier(s) présent(s), {supprimes} supprimé(s)"
                     + ("  (analyse interrompue)" if self.annuler.is_set() else ""))
            if lecteur.secteurs_illisibles:
                texte += f"  —  {lecteur.secteurs_illisibles} secteur(s) illisible(s)"
            self.file.put(("statut", texte))
            if not elements:
                self.file.put(("alerte", "Aucun fichier", "Aucun fichier trouvé par la recherche rapide.\n\n"
                                                          "Essayez la recherche approfondie (2e onglet)."))

        self.lancer("Analyse", travail)

    def afficher(self):
        """Remplit l'arbre selon les filtres."""
        self.arbre.delete(*self.arbre.get_children())
        texte = self.recherche.get().strip().lower()
        dossiers = {}
        n_aff = 0
        for i, e in sorted(enumerate(self.elements), key=lambda x: x[1].chemin.lower()):
            if (e.supprime and not self.voir_supprimes.get()) or (not e.supprime and not self.voir_presents.get()):
                continue
            if texte and texte not in e.chemin.lower():
                continue
            parties = e.chemin.split("/")
            parent = ""
            for k in range(len(parties) - 1):
                cle = "/".join(parties[:k + 1])
                if cle not in dossiers:
                    dossiers[cle] = self.arbre.insert(parent, "end", iid="d:" + cle, text="📁 " + parties[k],
                                                      open=k == 0 or bool(texte))
                parent = dossiers[cle]
            date = datetime.datetime.fromtimestamp(e.date).strftime("%d/%m/%Y %H:%M") if e.date else ""
            tags = ("faible",) if e.chance == "Faible" else (("supprime",) if e.supprime else ())
            self.arbre.insert(parent, "end", iid=f"f{i}", text=parties[-1], tags=tags,
                              values=(app.taille_lisible(e.taille), "Supprimé" if e.supprime else "Présent",
                                      e.chance, date))
            n_aff += 1
        self.resume.configure(text=f"{n_aff} fichier(s) affiché(s) sur {len(self.elements)}")

    def _fichiers_sous(self, iid, resultat):
        if iid.startswith("f"):
            resultat.add(int(iid[1:]))
        for enfant in self.arbre.get_children(iid):
            self._fichiers_sous(enfant, resultat)

    def recuperer_selection(self):
        indices = set()
        for iid in self.arbre.selection():
            self._fichiers_sous(iid, indices)
        if not indices:
            messagebox.showinfo("Sélection vide", "Sélectionnez des fichiers ou des dossiers dans la liste.",
                                parent=self)
            return
        self._recuperer([self.elements[i] for i in sorted(indices)])

    def recuperer_tout(self):
        indices = set()
        for iid in self.arbre.get_children(""):
            self._fichiers_sous(iid, indices)
        if not indices:
            messagebox.showinfo("Rien à récupérer", "Lancez d'abord l'analyse.", parent=self)
            return
        self._recuperer([self.elements[i] for i in sorted(indices)])

    def _recuperer(self, elements):
        if not self.fs:
            return
        total = sum(e.taille for e in elements)
        racine = self.demander_destination(f"Où enregistrer les {len(elements)} fichier(s) "
                                           f"({app.taille_lisible(total)}) ?")
        if not racine:
            return
        fs = self.fs

        def travail():
            faits, echecs, octets = 0, [], 0
            for i, e in enumerate(elements, 1):
                if self.annuler.is_set():
                    break
                try:
                    r.verifier_espace(racine)
                except r.EspaceInsuffisant as ex:
                    echecs.append(f"(arrêt) {ex}")
                    break
                sous = "Fichiers supprimés" if e.supprime else "Fichiers présents"
                try:
                    fs.extraire(e, os.path.join(racine, sous, e.chemin))
                    faits += 1
                except OSError as ex:
                    echecs.append(f"{e.chemin} : {ex}")
                octets += e.taille
                self.file.put(("progres", octets * 100 / max(total, 1)))
                self.file.put(("statut", f"Récupération : {i} / {len(elements)} — {e.chemin}"))
            for dossier, _, fichiers in os.walk(racine):
                app.rendre_a_l_utilisateur(dossier)
            if echecs:
                with open(os.path.join(racine, "fichiers_non_récupérés.txt"), "w") as f:
                    f.write("\n".join(echecs) + "\n")
            self.file.put(("dossier", racine))
            msg = f"{faits} fichier(s) récupéré(s) dans :\n{racine}"
            if echecs:
                msg += f"\n\n{len(echecs)} problème(s) : voir fichiers_non_récupérés.txt"
            if self.annuler.is_set():
                msg = "Récupération annulée.\n\n" + msg
            msg += ("\n\nVérifiez les fichiers supprimés : s'ils ont été en partie écrasés, ils peuvent "
                    "être incomplets.")
            self.file.put(("statut", f"{faits} fichier(s) récupéré(s)."))
            self.file.put(("info", "Récupération terminée", msg))

        self.lancer("Récupération", travail, determinee=True)

    # ------------------------------------------------------------------ #
    # Recherche approfondie
    # ------------------------------------------------------------------ #

    def approfondie(self):
        if not self.source_valide():
            return
        voulu = self.source_voulue()
        categories = [c for c, v in self.categories.items() if v.get()]
        if not categories:
            messagebox.showinfo("Types", "Cochez au moins un type de fichier.", parent=self)
            return
        racine = self.demander_destination("Où enregistrer les fichiers retrouvés ?")
        if not racine:
            return
        racine = os.path.join(racine, "Recherche approfondie")
        os.makedirs(racine, exist_ok=True)
        app.rendre_a_l_utilisateur(racine)

        def signaler(pos, taille, vitesse, trouves):
            reste = (taille - pos) / vitesse if vitesse else 0
            self.file.put(("progres", pos * 100 / taille))
            self.file.put(("statut", f"Recherche approfondie : {pos * 100 / taille:.1f} %  —  "
                                     f"{app.taille_lisible(vitesse)}/s  —  reste ~{int(reste // 3600)} h "
                                     f"{int(reste % 3600 // 60):02d} min  —  {sum(trouves.values())} fichier(s) trouvé(s)"))
            self.file.put(("approfondi", "   ".join(f"{c} : {n}" for c, n in trouves.items())))

        def travail():
            lecteur = self.ouvrir_lecteur(voulu)
            self.file.put(("dossier", racine))
            trouves, motif = r.recherche_approfondie(lecteur, racine, categories, signaler, self.annuler)
            for dossier, _, _ in os.walk(racine):
                app.rendre_a_l_utilisateur(dossier)
            resume = "   ".join(f"{c} : {n}" for c, n in trouves.items())
            self.file.put(("approfondi", resume))
            msg = f"{sum(trouves.values())} fichier(s) retrouvé(s) :\n{resume}\n\nDans : {racine}"
            if motif == "annulé":
                msg = "Recherche interrompue.\n\n" + msg
            elif motif == "espace":
                msg = "Recherche arrêtée : le disque de destination est plein.\n\n" + msg
            self.file.put(("statut", f"Recherche approfondie terminée : {sum(trouves.values())} fichier(s)."))
            self.file.put(("info", "Recherche approfondie", msg))

        self.lancer("Recherche approfondie", travail, determinee=True)

    def ouvrir_dossier(self):
        if self.dernier_dossier and os.path.isdir(self.dernier_dossier):
            subprocess.run(["open", self.dernier_dossier])
