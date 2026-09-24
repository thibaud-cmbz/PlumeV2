Réponses réelles de l'API YouTube Data v3 (capturées le 2026-09-24), anonymisées : identifiants
remplacés par des identifiants factices cohérents entre fichiers, titres, descriptions, URL et
mots-clés remplacés. Durées, dimensions du lecteur, langues, dates et statistiques sont d'origine.

- `channels.json` : `channels.list` (`contentDetails,snippet`), 2 chaînes.
- `playlist_items_p1.json`, `playlist_items_p2.json` : 2 pages de `playlistItems.list` de la
  première chaîne (`maxResults=5`).
- `videos.json` : `videos.list` (`snippet,contentDetails,statistics,player`, `maxHeight=1080`) ;
  `vid00000000` à `vid00000009` viennent des deux pages ci-dessus, `vid00000010` à `vid00000014`
  de la seconde chaîne (verticales).
- `search_channels.json` : `search.list` de type `channel`.
- `quota_exceeded.json` : **non capturé** (il faudrait épuiser le quota) ; corps d'erreur 403 au
  format documenté par l'API.
