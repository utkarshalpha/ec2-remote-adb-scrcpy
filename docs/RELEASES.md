# Local release archive

Built executables are kept locally under `versions/windows` and intentionally
excluded from Git. GitHub source history remains small, while every preserved
Windows build remains available on the development computer.

| Release | SHA-256 |
| --- | --- |
| V1.0 | `826A37F8BBB58FD937FD43B24833C581F3DCBAD514E89ACAD937DE5E1D37BDD7` |
| V1.1 | `3779061630A1297B1860AA1F491ADB5289A5CB739B15B8D70657A1E96DD251A9` |
| V2.0 | `74F8BC220AE6DD7AE31FBD335CCC86333D849784828AE5B1989A639BFBB0898A` |
| V2.1 | `976E8918D1E2BF36AE7FEDDBF368D8E3E23F6F3168641506986DC9FEAC52B86A` |
| V2.2 | `CF01F0CF34342009F7A5F09B151CE724F4F96EAA0FCB6D2C783FCC2452B61877` |
| V2.3 | `544E9CE094E66CE5B7966381BAED8859BE2060D052138C913D90B95615632956` |

Preview and legacy pre-version builds are stored under
`versions/windows/previews` and `versions/windows/legacy`.

New Windows builds are written to `versions/windows/v2.3`. The Apple Silicon
builder writes its `.app` and `.dmg` to `versions/macos/v2.4.0`.
