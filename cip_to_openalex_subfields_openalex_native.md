# Suggested CIP → OpenAlex subfield mapping (OpenAlex-native)

This is a fresh suggested mapping from the CIPs in `CIP codes bio.xlsx` to **OpenAlex subfield entities**.

Each mapping row uses the **actual OpenAlex subfield ID format** (`https://openalex.org/subfields/{id}`) plus the current OpenAlex subfield name, field, and domain.

## How to read `fit`
- **primary**: the CIP is a direct or very close undergraduate preparation path for the subfield
- **secondary**: the CIP is a good feeder, but broader than the subfield or only partly aligned
- **adjacent**: the CIP can plausibly feed the subfield, but is not subfield-specific

## Ground rules used here
- I favored **specific OpenAlex subfields** over miscellaneous buckets.
- I only kept a miscellaneous OpenAlex subfield when the CIP itself is very broad and a more specific recommendation would pretend to know too much.
- This is a **suggested training-path map**, not a claim that OpenAlex itself defines CIP mappings.
- For very broad catch-all CIPs, omission is intentional rather than pretending there is one clean target.

## Biology & Biomedical (core)
| CIP code | CIP title | OpenAlex subfield ID | OpenAlex subfield name | OpenAlex field | OpenAlex domain | fit | Why |
|---|---|---|---|---|---|---|---|
| 26.0101 | Biology/Biological Sciences, General | `https://openalex.org/subfields/1307` | Cell Biology | Biochemistry, Genetics and Molecular Biology | Life Sciences | secondary | Broad biology program; common feeder into cell biology. |
| 26.0101 | Biology/Biological Sciences, General | `https://openalex.org/subfields/1311` | Genetics | Biochemistry, Genetics and Molecular Biology | Life Sciences | secondary | Broad biology program; common feeder into genetics. |
| 26.0101 | Biology/Biological Sciences, General | `https://openalex.org/subfields/1312` | Molecular Biology | Biochemistry, Genetics and Molecular Biology | Life Sciences | secondary | Broad biology program; common feeder into molecular biology. |
| 26.0101 | Biology/Biological Sciences, General | `https://openalex.org/subfields/2404` | Microbiology | Immunology and Microbiology | Life Sciences | adjacent | Can feed microbiology, but not microbiology-specific. |
| 26.0102 | Biomedical Sciences, General | `https://openalex.org/subfields/1306` | Cancer Research | Biochemistry, Genetics and Molecular Biology | Life Sciences | secondary | Biomedical sciences is a common feeder into cancer biology. |
| 26.0102 | Biomedical Sciences, General | `https://openalex.org/subfields/1313` | Molecular Medicine | Biochemistry, Genetics and Molecular Biology | Life Sciences | primary | Biomedical sciences maps most directly to molecular medicine. |
| 26.0102 | Biomedical Sciences, General | `https://openalex.org/subfields/2403` | Immunology | Immunology and Microbiology | Life Sciences | secondary | Biomedical curricula often include strong immunology preparation. |
| 26.0102 | Biomedical Sciences, General | `https://openalex.org/subfields/3004` | Pharmacology | Pharmacology, Toxicology and Pharmaceutics | Life Sciences | adjacent | Some biomedical programs prepare students for pharmacology. |
## Biochem / Mol Bio / Biophysics
| CIP code | CIP title | OpenAlex subfield ID | OpenAlex subfield name | OpenAlex field | OpenAlex domain | fit | Why |
|---|---|---|---|---|---|---|---|
| 26.0202 | Biochemistry | `https://openalex.org/subfields/1303` | Biochemistry | Biochemistry, Genetics and Molecular Biology | Life Sciences | primary | Direct name match. |
| 26.0202 | Biochemistry | `https://openalex.org/subfields/1308` | Clinical Biochemistry | Biochemistry, Genetics and Molecular Biology | Life Sciences | secondary | Biochemistry is a common feeder into clinical biochemistry. |
| 26.0203 | Biophysics | `https://openalex.org/subfields/1304` | Biophysics | Biochemistry, Genetics and Molecular Biology | Life Sciences | primary | Direct name match. |
| 26.0203 | Biophysics | `https://openalex.org/subfields/1315` | Structural Biology | Biochemistry, Genetics and Molecular Biology | Life Sciences | secondary | Biophysics often overlaps with structural biology. |
| 26.0204 | Molecular Biology | `https://openalex.org/subfields/1311` | Genetics | Biochemistry, Genetics and Molecular Biology | Life Sciences | secondary | Molecular biology often overlaps with genetics. |
| 26.0204 | Molecular Biology | `https://openalex.org/subfields/1312` | Molecular Biology | Biochemistry, Genetics and Molecular Biology | Life Sciences | primary | Direct name match. |
| 26.0205 | Molecular Biochemistry | `https://openalex.org/subfields/1303` | Biochemistry | Biochemistry, Genetics and Molecular Biology | Life Sciences | primary | Biochemistry component is central. |
| 26.0205 | Molecular Biochemistry | `https://openalex.org/subfields/1312` | Molecular Biology | Biochemistry, Genetics and Molecular Biology | Life Sciences | secondary | Also trains for molecular biology. |
| 26.0206 | Molecular Biophysics | `https://openalex.org/subfields/1304` | Biophysics | Biochemistry, Genetics and Molecular Biology | Life Sciences | primary | Direct and close match to biophysics. |
| 26.0206 | Molecular Biophysics | `https://openalex.org/subfields/1315` | Structural Biology | Biochemistry, Genetics and Molecular Biology | Life Sciences | secondary | Molecular biophysics often overlaps with structural biology. |
| 26.0207 | Structural Biology | `https://openalex.org/subfields/1315` | Structural Biology | Biochemistry, Genetics and Molecular Biology | Life Sciences | primary | Direct name match. |
| 26.0207 | Structural Biology | `https://openalex.org/subfields/1304` | Biophysics | Biochemistry, Genetics and Molecular Biology | Life Sciences | secondary | Structural biology uses biophysical methods (cryo-EM, NMR, crystallography). |
| 26.0208 | Photobiology | `https://openalex.org/subfields/1304` | Biophysics | Biochemistry, Genetics and Molecular Biology | Life Sciences | primary | Photobiology IS biophysics in OpenAlex's taxonomy; only mapping for this CIP. |
| 26.0209 | Radiation Biology/Radiobiology | `https://openalex.org/subfields/1306` | Cancer Research | Biochemistry, Genetics and Molecular Biology | Life Sciences | secondary | Radiobiology is often a feeder into cancer research. |
| 26.0209 | Radiation Biology/Radiobiology | `https://openalex.org/subfields/2307` | Health, Toxicology and Mutagenesis | Environmental Science | Physical Sciences | secondary | Radiation biology also aligns with toxicology/mutagenesis. |
| 26.0210 | Biochemistry and Molecular Biology | `https://openalex.org/subfields/1303` | Biochemistry | Biochemistry, Genetics and Molecular Biology | Life Sciences | primary | Program explicitly includes biochemistry. |
| 26.0210 | Biochemistry and Molecular Biology | `https://openalex.org/subfields/1312` | Molecular Biology | Biochemistry, Genetics and Molecular Biology | Life Sciences | primary | Program explicitly includes molecular biology. |
| 26.0299 | Biochemistry, Biophysics and Molecular Biology, Other | `https://openalex.org/subfields/1303` | Biochemistry | Biochemistry, Genetics and Molecular Biology | Life Sciences | secondary | Umbrella 'other' program with strong biochemistry overlap. |
| 26.0299 | Biochemistry, Biophysics and Molecular Biology, Other | `https://openalex.org/subfields/1304` | Biophysics | Biochemistry, Genetics and Molecular Biology | Life Sciences | secondary | Umbrella 'other' program with strong biophysics overlap. |
| 26.0299 | Biochemistry, Biophysics and Molecular Biology, Other | `https://openalex.org/subfields/1312` | Molecular Biology | Biochemistry, Genetics and Molecular Biology | Life Sciences | secondary | Umbrella 'other' program with strong molecular biology overlap. |
## Cell / Anatomy / Dev Bio
| CIP code | CIP title | OpenAlex subfield ID | OpenAlex subfield name | OpenAlex field | OpenAlex domain | fit | Why |
|---|---|---|---|---|---|---|---|
| 26.0401 | Cell/Cellular Biology and Histology | `https://openalex.org/subfields/1307` | Cell Biology | Biochemistry, Genetics and Molecular Biology | Life Sciences | primary | Cell biology is central. |
| 26.0401 | Cell/Cellular Biology and Histology | `https://openalex.org/subfields/2702` | Anatomy | Medicine | Health Sciences | secondary | Histology is microscopic anatomy; 2702 Anatomy is the closest valid OpenAlex subfield. |
| 26.0403 | Anatomy | `https://openalex.org/subfields/2702` | Anatomy | Medicine | Health Sciences | primary | Direct name match to anatomy. |
| 26.0404 | Developmental Biology and Embryology | `https://openalex.org/subfields/1309` | Developmental Biology | Biochemistry, Genetics and Molecular Biology | Life Sciences | primary | Direct name match on developmental biology; encompasses embryology. |
| 26.0406 | Cell/Cellular and Molecular Biology | `https://openalex.org/subfields/1307` | Cell Biology | Biochemistry, Genetics and Molecular Biology | Life Sciences | primary | Program explicitly includes cell biology. |
| 26.0406 | Cell/Cellular and Molecular Biology | `https://openalex.org/subfields/1312` | Molecular Biology | Biochemistry, Genetics and Molecular Biology | Life Sciences | primary | Program explicitly includes molecular biology. |
| 26.0407 | Cell Biology and Anatomy | `https://openalex.org/subfields/1307` | Cell Biology | Biochemistry, Genetics and Molecular Biology | Life Sciences | primary | Cell biology is explicit. |
| 26.0407 | Cell Biology and Anatomy | `https://openalex.org/subfields/2702` | Anatomy | Medicine | Health Sciences | secondary | Also trains for anatomy. |
| 26.0499 | Cell/Cellular Biology and Anatomical Sciences, Other | `https://openalex.org/subfields/1307` | Cell Biology | Biochemistry, Genetics and Molecular Biology | Life Sciences | secondary | Broad 'other' cell/anatomical sciences program; cell biology is a common destination. |
| 26.0499 | Cell/Cellular Biology and Anatomical Sciences, Other | `https://openalex.org/subfields/2702` | Anatomy | Medicine | Health Sciences | secondary | Broad 'other' cell/anatomical sciences program; anatomy is a common destination. |
## Microbiology / Immunology / Infectious disease
| CIP code | CIP title | OpenAlex subfield ID | OpenAlex subfield name | OpenAlex field | OpenAlex domain | fit | Why |
|---|---|---|---|---|---|---|---|
| 26.0502 | Microbiology, General | `https://openalex.org/subfields/2404` | Microbiology | Immunology and Microbiology | Life Sciences | primary | Direct name match. |
| 26.0502 | Microbiology, General | `https://openalex.org/subfields/2402` | Applied Microbiology and Biotechnology | Immunology and Microbiology | Life Sciences | secondary | General micro grads feed into applied microbiology research. |
| 26.0503 | Medical Microbiology and Bacteriology | `https://openalex.org/subfields/2404` | Microbiology | Immunology and Microbiology | Life Sciences | primary | Medical microbiology is still microbiology at core. |
| 26.0503 | Medical Microbiology and Bacteriology | `https://openalex.org/subfields/2725` | Infectious Diseases | Medicine | Health Sciences | secondary | Medical microbiology/bacteriology feeds infectious-disease research. |
| 26.0504 | Virology | `https://openalex.org/subfields/2406` | Virology | Immunology and Microbiology | Life Sciences | primary | Direct name match. |
| 26.0504 | Virology | `https://openalex.org/subfields/2725` | Infectious Diseases | Medicine | Health Sciences | secondary | Virology research heavily overlaps infectious disease. |
| 26.0505 | Parasitology | `https://openalex.org/subfields/2405` | Parasitology | Immunology and Microbiology | Life Sciences | primary | Direct name match. |
| 26.0505 | Parasitology | `https://openalex.org/subfields/2725` | Infectious Diseases | Medicine | Health Sciences | secondary | Parasitology research overlaps tropical/infectious disease. |
| 26.0506 | Mycology | `https://openalex.org/subfields/2404` | Microbiology | Immunology and Microbiology | Life Sciences | primary | Mycology (fungi) is a core branch of microbiology. |
| 26.0507 | Immunology | `https://openalex.org/subfields/2403` | Immunology | Immunology and Microbiology | Life Sciences | primary | Direct name match. |
| 26.0507 | Immunology | `https://openalex.org/subfields/2725` | Infectious Diseases | Medicine | Health Sciences | adjacent | Immunology intersects infectious disease but isn't the core. |
| 26.0508 | Microbiology and Immunology | `https://openalex.org/subfields/2403` | Immunology | Immunology and Microbiology | Life Sciences | primary | Program explicitly includes immunology. |
| 26.0508 | Microbiology and Immunology | `https://openalex.org/subfields/2404` | Microbiology | Immunology and Microbiology | Life Sciences | primary | Program explicitly includes microbiology. |
| 26.0509 | Infectious Disease and Global Health | `https://openalex.org/subfields/2713` | Epidemiology | Medicine | Health Sciences | secondary | Global-health tracks often include epidemiology. |
| 26.0509 | Infectious Disease and Global Health | `https://openalex.org/subfields/2725` | Infectious Diseases | Medicine | Health Sciences | primary | Direct infectious-disease alignment. |
| 26.0509 | Infectious Disease and Global Health | `https://openalex.org/subfields/2739` | Public Health, Environmental and Occupational Health | Medicine | Health Sciences | secondary | Global health also overlaps public-health research. |
| 26.0599 | Microbiological Sciences and Immunology, Other | `https://openalex.org/subfields/2403` | Immunology | Immunology and Microbiology | Life Sciences | secondary | Umbrella 'other' program with immunology overlap. |
| 26.0599 | Microbiological Sciences and Immunology, Other | `https://openalex.org/subfields/2404` | Microbiology | Immunology and Microbiology | Life Sciences | secondary | Umbrella 'other' program with microbiology overlap. |
## Genetics / Genomics
| CIP code | CIP title | OpenAlex subfield ID | OpenAlex subfield name | OpenAlex field | OpenAlex domain | fit | Why |
|---|---|---|---|---|---|---|---|
| 26.0801 | Genetics, General | `https://openalex.org/subfields/1311` | Genetics | Biochemistry, Genetics and Molecular Biology | Life Sciences | primary | Direct name match. |
| 26.0801 | Genetics, General | `https://openalex.org/subfields/1312` | Molecular Biology | Biochemistry, Genetics and Molecular Biology | Life Sciences | secondary | Genetics programs extensively use molecular biology techniques. |
| 26.0802 | Molecular Genetics | `https://openalex.org/subfields/1311` | Genetics | Biochemistry, Genetics and Molecular Biology | Life Sciences | primary | Direct genetics alignment. |
| 26.0802 | Molecular Genetics | `https://openalex.org/subfields/1312` | Molecular Biology | Biochemistry, Genetics and Molecular Biology | Life Sciences | secondary | Molecular genetics also overlaps molecular biology. |
| 26.0803 | Microbial and Eukaryotic Genetics | `https://openalex.org/subfields/1311` | Genetics | Biochemistry, Genetics and Molecular Biology | Life Sciences | primary | Still fundamentally a genetics program. |
| 26.0804 | Animal Genetics | `https://openalex.org/subfields/1311` | Genetics | Biochemistry, Genetics and Molecular Biology | Life Sciences | primary | Animal genetics is a genetics specialty. |
| 26.0804 | Animal Genetics | `https://openalex.org/subfields/1103` | Animal Science and Zoology | Agricultural and Biological Sciences | Life Sciences | secondary | Animal genetics grads work in animal science research. |
| 26.0805 | Plant Genetics | `https://openalex.org/subfields/1311` | Genetics | Biochemistry, Genetics and Molecular Biology | Life Sciences | primary | Plant genetics is a genetics specialty. |
| 26.0805 | Plant Genetics | `https://openalex.org/subfields/1110` | Plant Science | Agricultural and Biological Sciences | Life Sciences | secondary | Plant genetics grads work in plant science research. |
| 26.0806 | Human/Medical Genetics | `https://openalex.org/subfields/1311` | Genetics | Biochemistry, Genetics and Molecular Biology | Life Sciences | primary | Human/medical genetics maps directly to genetics. |
| 26.0806 | Human/Medical Genetics | `https://openalex.org/subfields/1313` | Molecular Medicine | Biochemistry, Genetics and Molecular Biology | Life Sciences | secondary | Medical genetics often feeds molecular medicine. |
| 26.0807 | Genome Sciences/Genomics | `https://openalex.org/subfields/1311` | Genetics | Biochemistry, Genetics and Molecular Biology | Life Sciences | primary | Genomics fits most directly under genetics. |
| 26.0807 | Genome Sciences/Genomics | `https://openalex.org/subfields/1312` | Molecular Biology | Biochemistry, Genetics and Molecular Biology | Life Sciences | secondary | Genomics also overlaps molecular biology. |
| 26.0899 | Genetics, Other | `https://openalex.org/subfields/1311` | Genetics | Biochemistry, Genetics and Molecular Biology | Life Sciences | secondary | Broad 'genetics, other' bucket; genetics is still the best fit. |
## Physiology / Pathology / related
| CIP code | CIP title | OpenAlex subfield ID | OpenAlex subfield name | OpenAlex field | OpenAlex domain | fit | Why |
|---|---|---|---|---|---|---|---|
| 26.0901 | Physiology, General | `https://openalex.org/subfields/1314` | Physiology | Biochemistry, Genetics and Molecular Biology | Life Sciences | primary | Direct name match. |
| 26.0901 | Physiology, General | `https://openalex.org/subfields/1310` | Endocrinology | Biochemistry, Genetics and Molecular Biology | Life Sciences | secondary | General physiology programs heavily overlap endocrinology. |
| 26.0902 | Molecular Physiology | `https://openalex.org/subfields/1312` | Molecular Biology | Biochemistry, Genetics and Molecular Biology | Life Sciences | secondary | Molecular physiology also overlaps molecular biology. |
| 26.0902 | Molecular Physiology | `https://openalex.org/subfields/1314` | Physiology | Biochemistry, Genetics and Molecular Biology | Life Sciences | primary | Molecular physiology still maps most directly to physiology. |
| 26.0903 | Cell Physiology | `https://openalex.org/subfields/1307` | Cell Biology | Biochemistry, Genetics and Molecular Biology | Life Sciences | secondary | Also overlaps cell biology. |
| 26.0903 | Cell Physiology | `https://openalex.org/subfields/1314` | Physiology | Biochemistry, Genetics and Molecular Biology | Life Sciences | primary | Cell physiology is a physiology specialty. |
| 26.0911 | Oncology and Cancer Biology | `https://openalex.org/subfields/1306` | Cancer Research | Biochemistry, Genetics and Molecular Biology | Life Sciences | primary | Direct cancer-research alignment. |
| 26.0911 | Oncology and Cancer Biology | `https://openalex.org/subfields/1313` | Molecular Medicine | Biochemistry, Genetics and Molecular Biology | Life Sciences | secondary | Cancer biology often overlaps molecular medicine. |
## Pharm / Tox (biology-side)
| CIP code | CIP title | OpenAlex subfield ID | OpenAlex subfield name | OpenAlex field | OpenAlex domain | fit | Why |
|---|---|---|---|---|---|---|---|
| 26.1001 | Pharmacology | `https://openalex.org/subfields/3004` | Pharmacology | Pharmacology, Toxicology and Pharmaceutics | Life Sciences | primary | Direct name match. |
| 26.1001 | Pharmacology | `https://openalex.org/subfields/3002` | Drug Discovery | Pharmacology, Toxicology and Pharmaceutics | Life Sciences | secondary | Pharmacology grads contribute to drug discovery research. |
| 26.1002 | Molecular Pharmacology | `https://openalex.org/subfields/1312` | Molecular Biology | Biochemistry, Genetics and Molecular Biology | Life Sciences | secondary | Strong molecular-biology overlap. |
| 26.1002 | Molecular Pharmacology | `https://openalex.org/subfields/3004` | Pharmacology | Pharmacology, Toxicology and Pharmaceutics | Life Sciences | primary | Molecular pharmacology still maps most directly to pharmacology. |
| 26.1003 | Neuropharmacology | `https://openalex.org/subfields/2804` | Cellular and Molecular Neuroscience | Neuroscience | Life Sciences | secondary | Also overlaps cellular and molecular neuroscience. |
| 26.1003 | Neuropharmacology | `https://openalex.org/subfields/3004` | Pharmacology | Pharmacology, Toxicology and Pharmaceutics | Life Sciences | primary | Neuropharmacology is a pharmacology specialty. |
| 26.1004 | Toxicology | `https://openalex.org/subfields/2307` | Health, Toxicology and Mutagenesis | Environmental Science | Physical Sciences | secondary | Toxicology also overlaps OA's toxicology/mutagenesis subfield. |
| 26.1004 | Toxicology | `https://openalex.org/subfields/3005` | Toxicology | Pharmacology, Toxicology and Pharmaceutics | Life Sciences | primary | Direct name match. |
| 26.1005 | Molecular Toxicology | `https://openalex.org/subfields/1312` | Molecular Biology | Biochemistry, Genetics and Molecular Biology | Life Sciences | secondary | Strong molecular-biology overlap. |
| 26.1005 | Molecular Toxicology | `https://openalex.org/subfields/2307` | Health, Toxicology and Mutagenesis | Environmental Science | Physical Sciences | secondary | Also overlaps toxicology/mutagenesis. |
| 26.1005 | Molecular Toxicology | `https://openalex.org/subfields/3005` | Toxicology | Pharmacology, Toxicology and Pharmaceutics | Life Sciences | primary | Molecular toxicology still maps most directly to toxicology. |
## Bioinformatics / Biostats / Computational bio
| CIP code | CIP title | OpenAlex subfield ID | OpenAlex subfield name | OpenAlex field | OpenAlex domain | fit | Why |
|---|---|---|---|---|---|---|---|
| 26.1101 | Biometry/Biometrics | `https://openalex.org/subfields/2613` | Statistics and Probability | Mathematics | Physical Sciences | primary | Biometry/biometrics is best treated as applied statistics. |
| 26.1101 | Biometry/Biometrics | `https://openalex.org/subfields/2713` | Epidemiology | Medicine | Health Sciences | adjacent | Can feed epidemiology, but not epidemiology-specific. |
| 26.1102 | Biostatistics | `https://openalex.org/subfields/2613` | Statistics and Probability | Mathematics | Physical Sciences | primary | Biostatistics maps most directly to statistics and probability. |
| 26.1102 | Biostatistics | `https://openalex.org/subfields/2713` | Epidemiology | Medicine | Health Sciences | secondary | Biostatistics is a common feeder into epidemiology. |
| 26.1103 | Bioinformatics | `https://openalex.org/subfields/1311` | Genetics | Biochemistry, Genetics and Molecular Biology | Life Sciences | primary | Bioinformatics research output is overwhelmingly classified under genetics/genomics in OpenAlex. |
| 26.1103 | Bioinformatics | `https://openalex.org/subfields/1702` | Artificial Intelligence | Computer Science | Physical Sciences | secondary | Some bioinformatics programs also overlap AI methods. |
| 26.1103 | Bioinformatics | `https://openalex.org/subfields/1703` | Computational Theory and Mathematics | Computer Science | Physical Sciences | secondary | Computational methods are secondary to the biological application domain. |
| 26.1104 | Computational Biology | `https://openalex.org/subfields/1311` | Genetics | Biochemistry, Genetics and Molecular Biology | Life Sciences | primary | Computational biology papers land predominantly in genetics/genomics. |
| 26.1104 | Computational Biology | `https://openalex.org/subfields/1312` | Molecular Biology | Biochemistry, Genetics and Molecular Biology | Life Sciences | secondary | Often applied to molecular biology/genomics. |
| 26.1104 | Computational Biology | `https://openalex.org/subfields/1703` | Computational Theory and Mathematics | Computer Science | Physical Sciences | secondary | Computational methods are secondary to the biological application domain. |
| 26.1199 | Biomathematics, Bioinformatics, and Computational Biology, Other | `https://openalex.org/subfields/1703` | Computational Theory and Mathematics | Computer Science | Physical Sciences | secondary | Broad quantitative-bio umbrella; computational methods are central. |
| 26.1199 | Biomathematics, Bioinformatics, and Computational Biology, Other | `https://openalex.org/subfields/2613` | Statistics and Probability | Mathematics | Physical Sciences | secondary | Statistics is also a common destination. |
## Biotech
| CIP code | CIP title | OpenAlex subfield ID | OpenAlex subfield name | OpenAlex field | OpenAlex domain | fit | Why |
|---|---|---|---|---|---|---|---|
| 26.1201 | Biotechnology | `https://openalex.org/subfields/1305` | Biotechnology | Biochemistry, Genetics and Molecular Biology | Life Sciences | primary | Direct name match to biotechnology. |
| 26.1201 | Biotechnology | `https://openalex.org/subfields/1502` | Bioengineering | Chemical Engineering | Physical Sciences | adjacent | Some programs also feed bioengineering. |
| 26.1201 | Biotechnology | `https://openalex.org/subfields/2402` | Applied Microbiology and Biotechnology | Immunology and Microbiology | Life Sciences | secondary | Also aligns with applied microbiology and biotechnology. |
## Ecology / Evolution / Marine / Environment-bio
| CIP code | CIP title | OpenAlex subfield ID | OpenAlex subfield name | OpenAlex field | OpenAlex domain | fit | Why |
|---|---|---|---|---|---|---|---|
| 26.1301 | Ecology | `https://openalex.org/subfields/1105` | Ecology, Evolution, Behavior and Systematics | Agricultural and Biological Sciences | Life Sciences | primary | Ecology maps most directly to the life-sciences ecology/evolution/systematics bucket. |
| 26.1301 | Ecology | `https://openalex.org/subfields/2303` | Ecology | Environmental Science | Physical Sciences | secondary | Also overlaps the environmental-science ecology subfield. |
| 26.1302 | Marine Biology and Biological Oceanography | `https://openalex.org/subfields/1104` | Aquatic Science | Agricultural and Biological Sciences | Life Sciences | primary | Direct aquatic/marine life-sciences alignment. |
| 26.1302 | Marine Biology and Biological Oceanography | `https://openalex.org/subfields/1910` | Oceanography | Earth and Planetary Sciences | Physical Sciences | secondary | Biological oceanography also overlaps oceanography. |
| 26.1303 | Evolutionary Biology | `https://openalex.org/subfields/1105` | Ecology, Evolution, Behavior and Systematics | Agricultural and Biological Sciences | Life Sciences | primary | Evolutionary biology fits directly in the ecology/evolution/systematics bucket. |
| 26.1303 | Evolutionary Biology | `https://openalex.org/subfields/1311` | Genetics | Biochemistry, Genetics and Molecular Biology | Life Sciences | secondary | Modern evo bio is heavily genetics-based (population genetics, phylogenomics). |
| 26.1304 | Aquatic Biology/Limnology | `https://openalex.org/subfields/1104` | Aquatic Science | Agricultural and Biological Sciences | Life Sciences | primary | Aquatic biology maps directly to aquatic science. |
| 26.1304 | Aquatic Biology/Limnology | `https://openalex.org/subfields/2312` | Water Science and Technology | Environmental Science | Physical Sciences | secondary | Limnology also overlaps water science. |
| 26.1305 | Environmental Biology | `https://openalex.org/subfields/1105` | Ecology, Evolution, Behavior and Systematics | Agricultural and Biological Sciences | Life Sciences | primary | Environmental biology is best placed in the biological ecology/evolution bucket. |
| 26.1305 | Environmental Biology | `https://openalex.org/subfields/2303` | Ecology | Environmental Science | Physical Sciences | secondary | Also overlaps environmental-science ecology. |
| 26.1306 | Population Biology | `https://openalex.org/subfields/1105` | Ecology, Evolution, Behavior and Systematics | Agricultural and Biological Sciences | Life Sciences | primary | Population biology fits the ecology/evolution/systematics bucket. |
| 26.1307 | Conservation Biology | `https://openalex.org/subfields/1105` | Ecology, Evolution, Behavior and Systematics | Agricultural and Biological Sciences | Life Sciences | secondary | Also overlaps ecology/evolution/systematics. |
| 26.1307 | Conservation Biology | `https://openalex.org/subfields/2309` | Nature and Landscape Conservation | Environmental Science | Physical Sciences | primary | Conservation biology aligns directly with nature and landscape conservation. |
| 26.1308 | Systematic Biology/Biological Systematics | `https://openalex.org/subfields/1105` | Ecology, Evolution, Behavior and Systematics | Agricultural and Biological Sciences | Life Sciences | primary | Systematics is part of the ecology/evolution/systematics bucket. |
| 26.1309 | Epidemiology | `https://openalex.org/subfields/2713` | Epidemiology | Medicine | Health Sciences | primary | Direct name match to epidemiology. |
| 26.1309 | Epidemiology | `https://openalex.org/subfields/2739` | Public Health, Environmental and Occupational Health | Medicine | Health Sciences | secondary | Also overlaps public health. |
| 26.1310 | Ecology and Evolutionary Biology | `https://openalex.org/subfields/1105` | Ecology, Evolution, Behavior and Systematics | Agricultural and Biological Sciences | Life Sciences | primary | Combined ecology/evolution program maps directly to the ecology/evolution/systematics bucket. |
| 26.1311 | Epidemiology and Biostatistics | `https://openalex.org/subfields/2613` | Statistics and Probability | Mathematics | Physical Sciences | secondary | Biostatistics is also explicit. |
| 26.1311 | Epidemiology and Biostatistics | `https://openalex.org/subfields/2713` | Epidemiology | Medicine | Health Sciences | primary | Epidemiology is explicit. |
| 26.1399 | Ecology, Evolution, Systematics and Population Biology, Other | `https://openalex.org/subfields/1105` | Ecology, Evolution, Behavior and Systematics | Agricultural and Biological Sciences | Life Sciences | secondary | Broad 'other' ecology/evolution/systematics umbrella. |
## Molecular medicine
| CIP code | CIP title | OpenAlex subfield ID | OpenAlex subfield name | OpenAlex field | OpenAlex domain | fit | Why |
|---|---|---|---|---|---|---|---|
| 26.1401 | Molecular Medicine | `https://openalex.org/subfields/1313` | Molecular Medicine | Biochemistry, Genetics and Molecular Biology | Life Sciences | primary | Direct name match. |
| 26.1401 | Molecular Medicine | `https://openalex.org/subfields/1311` | Genetics | Biochemistry, Genetics and Molecular Biology | Life Sciences | secondary | Molecular medicine leverages genetics/genomics extensively. |
| 26.1401 | Molecular Medicine | `https://openalex.org/subfields/1306` | Cancer Research | Biochemistry, Genetics and Molecular Biology | Life Sciences | secondary | Cancer is a major application domain for molecular medicine. |
## Neurobiology / Neuroscience (biology-side)
| CIP code | CIP title | OpenAlex subfield ID | OpenAlex subfield name | OpenAlex field | OpenAlex domain | fit | Why |
|---|---|---|---|---|---|---|---|
| 26.1501 | Neuroscience | `https://openalex.org/subfields/2802` | Behavioral Neuroscience | Neuroscience | Life Sciences | secondary | General neuro programs produce behavioral neuro research. |
| 26.1501 | Neuroscience | `https://openalex.org/subfields/2804` | Cellular and Molecular Neuroscience | Neuroscience | Life Sciences | secondary | General neuroscience spans behavioral, cognitive, and cellular — C&M is one slice. |
| 26.1501 | Neuroscience | `https://openalex.org/subfields/2805` | Cognitive Neuroscience | Neuroscience | Life Sciences | secondary | General neuro programs produce cognitive neuro research. |
| 26.1502 | Neuroanatomy | `https://openalex.org/subfields/2702` | Anatomy | Medicine | Health Sciences | secondary | Also overlaps anatomy. |
| 26.1502 | Neuroanatomy | `https://openalex.org/subfields/2804` | Cellular and Molecular Neuroscience | Neuroscience | Life Sciences | primary | Neuroanatomy fits best within cellular and molecular neuroscience. |
| 26.1503 | Neurobiology and Anatomy | `https://openalex.org/subfields/2702` | Anatomy | Medicine | Health Sciences | secondary | Anatomy remains a meaningful secondary fit. |
| 26.1503 | Neurobiology and Anatomy | `https://openalex.org/subfields/2804` | Cellular and Molecular Neuroscience | Neuroscience | Life Sciences | primary | Neurobiology and anatomy maps best to cellular and molecular neuroscience. |
| 26.1504 | Neurobiology and Behavior | `https://openalex.org/subfields/2802` | Behavioral Neuroscience | Neuroscience | Life Sciences | primary | Behavior is explicit, making behavioral neuroscience the best fit. |
| 26.1504 | Neurobiology and Behavior | `https://openalex.org/subfields/2804` | Cellular and Molecular Neuroscience | Neuroscience | Life Sciences | secondary | Still overlaps neurobiology. |
| 26.1599 | Neurobiology and Neurosciences, Other | `https://openalex.org/subfields/2804` | Cellular and Molecular Neuroscience | Neuroscience | Life Sciences | primary | Broadest neuro subfield; best fit for catch-all neuroscience CIP. |
| 26.1599 | Neurobiology and Neurosciences, Other | `https://openalex.org/subfields/2802` | Behavioral Neuroscience | Neuroscience | Life Sciences | adjacent | Broadens this CIP beyond just cellular/molecular neuro. |
## Math/Stats + quant bio (adjacent)
| CIP code | CIP title | OpenAlex subfield ID | OpenAlex subfield name | OpenAlex field | OpenAlex domain | fit | Why |
|---|---|---|---|---|---|---|---|
| 27.0306 | Mathematical Biology | `https://openalex.org/subfields/1703` | Computational Theory and Mathematics | Computer Science | Physical Sciences | secondary | Also overlaps computational/theoretical methods. |
| 27.0306 | Mathematical Biology | `https://openalex.org/subfields/2613` | Statistics and Probability | Mathematics | Physical Sciences | primary | Mathematical biology is best treated as quantitative/statistical modeling. |
| 27.0501 | Statistics, General | `https://openalex.org/subfields/2613` | Statistics and Probability | Mathematics | Physical Sciences | primary | Direct statistics alignment. |
| 27.0502 | Mathematical Statistics and Probability | `https://openalex.org/subfields/2613` | Statistics and Probability | Mathematics | Physical Sciences | primary | Direct statistics-and-probability alignment. |
| 27.0503 | Mathematics and Statistics | `https://openalex.org/subfields/2613` | Statistics and Probability | Mathematics | Physical Sciences | primary | Combined mathematics/statistics programs remain closest to statistics. |
| 27.0599 | Statistics, Other | `https://openalex.org/subfields/2613` | Statistics and Probability | Mathematics | Physical Sciences | primary | Other statistics programs still fit the statistics subfield best. |
| 27.0601 | Applied Statistics, General | `https://openalex.org/subfields/2613` | Statistics and Probability | Mathematics | Physical Sciences | primary | Applied statistics programs map directly to statistics and probability. |
## Neuro + mind/behavior adjacent
| CIP code | CIP title | OpenAlex subfield ID | OpenAlex subfield name | OpenAlex field | OpenAlex domain | fit | Why |
|---|---|---|---|---|---|---|---|
| 30.1001 | Biopsychology | `https://openalex.org/subfields/2802` | Behavioral Neuroscience | Neuroscience | Life Sciences | secondary | Also overlaps behavioral neuroscience. |
| 30.1001 | Biopsychology | `https://openalex.org/subfields/3206` | Neuropsychology and Physiological Psychology | Psychology | Social Sciences | primary | Biopsychology maps most directly to neuropsychology and physiological psychology. |
| 30.2501 | Cognitive Science | `https://openalex.org/subfields/2805` | Cognitive Neuroscience | Neuroscience | Life Sciences | primary | Cognitive science maps cleanly to cognitive neuroscience. |
| 30.2501 | Cognitive Science | `https://openalex.org/subfields/3205` | Experimental and Cognitive Psychology | Psychology | Social Sciences | secondary | Also overlaps experimental and cognitive psychology. |
| 42.0101 | Psychology, General | `https://openalex.org/subfields/3205` | Experimental and Cognitive Psychology | Psychology | Social Sciences | secondary | General psychology can feed experimental/cognitive psychology. |
| 42.0101 | Psychology, General | `https://openalex.org/subfields/3206` | Neuropsychology and Physiological Psychology | Psychology | Social Sciences | adjacent | Can feed physiological psychology, but not specifically targeted. |
| 42.2706 | Behavioral Neuroscience | `https://openalex.org/subfields/2802` | Behavioral Neuroscience | Neuroscience | Life Sciences | primary | Direct alignment to behavioral neuroscience. |
| 42.2706 | Behavioral Neuroscience | `https://openalex.org/subfields/3206` | Neuropsychology and Physiological Psychology | Psychology | Social Sciences | secondary | Also overlaps neuropsychology/physiological psychology. |
## Anthropology adjacent
| CIP code | CIP title | OpenAlex subfield ID | OpenAlex subfield name | OpenAlex field | OpenAlex domain | fit | Why |
|---|---|---|---|---|---|---|---|
| 45.0202 | Physical and Biological Anthropology | `https://openalex.org/subfields/3314` | Anthropology | Social Sciences | Social Sciences | primary | Direct alignment to anthropology. |
| 45.0202 | Physical and Biological Anthropology | `https://openalex.org/subfields/1105` | Ecology, Evolution, Behavior and Systematics | Agricultural and Biological Sciences | Life Sciences | secondary | Human evolution, primatology, paleoecology — EEBS is the primary OA home for evo bio research underlying biological anthropology. |
| 45.0202 | Physical and Biological Anthropology | `https://openalex.org/subfields/1311` | Genetics | Biochemistry, Genetics and Molecular Biology | Life Sciences | secondary | Population genetics, ancient DNA, human genomics — ancient DNA revolution makes genetics central to modern bioanthropology. |
| 45.0202 | Physical and Biological Anthropology | `https://openalex.org/subfields/3302` | Archeology | Social Sciences | Social Sciences | secondary | Bioarchaeology, paleoanthropology, osteology — archaeological methods are integral to physical anthropology field work. |
| 45.0202 | Physical and Biological Anthropology | `https://openalex.org/subfields/2802` | Behavioral Neuroscience | Neuroscience | Life Sciences | adjacent | Neuroanthropology, primate cognition, comparative brain evolution. |
| 45.0202 | Physical and Biological Anthropology | `https://openalex.org/subfields/2713` | Epidemiology | Medicine | Health Sciences | adjacent | Disease ecology, One Health framework, epidemiology of ancient populations. |
| 45.0203 | Medical Anthropology | `https://openalex.org/subfields/3314` | Anthropology | Social Sciences | Social Sciences | primary | Medical anthropology remains anthropology at core. |
| 45.0203 | Medical Anthropology | `https://openalex.org/subfields/2739` | Public Health, Environmental and Occupational Health | Medicine | Health Sciences | secondary | Medical anthropology IS public health research — upgraded from adjacent. |
| 45.0203 | Medical Anthropology | `https://openalex.org/subfields/2713` | Epidemiology | Medicine | Health Sciences | secondary | Disease patterns across populations, social determinants of health. |
| 45.0203 | Medical Anthropology | `https://openalex.org/subfields/3306` | Health | Social Sciences | Social Sciences | secondary | Health systems, health equity, global health policy — social-science framing of health. |
| 45.0203 | Medical Anthropology | `https://openalex.org/subfields/1105` | Ecology, Evolution, Behavior and Systematics | Agricultural and Biological Sciences | Life Sciences | adjacent | Evolutionary medicine framework, host-pathogen coevolution in human populations. |
## Bio/biomedical engineering adjacent
| CIP code | CIP title | OpenAlex subfield ID | OpenAlex subfield name | OpenAlex field | OpenAlex domain | fit | Why |
|---|---|---|---|---|---|---|---|
| 14.0702 | Chemical and Biomolecular Engineering | `https://openalex.org/subfields/1502` | Bioengineering | Chemical Engineering | Physical Sciences | primary | Biomolecular engineering maps best to bioengineering. |
| 14.0702 | Chemical and Biomolecular Engineering | `https://openalex.org/subfields/1508` | Process Chemistry and Technology | Chemical Engineering | Physical Sciences | secondary | Chemical process training can also feed process chemistry/technology. |
## Omitted broad CIP

| CIP code | CIP title | Why omitted |
|---|---|---|
| 26.9999 | Biological and Biomedical Sciences, Other | Too broad and heterogeneous to recommend a specific subfield without institution-level context or course-level detail. |
