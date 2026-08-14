#!/usr/bin/env bash -C -e -u -o pipefail
configureStrelkaGermlineWorkflow.py \
    --bam test.recal.cram \
    --referenceFasta genome.fasta \
    --callRegions chr22_1-40001.bed.gz \
     \
    --runDir strelka

sed -i s/"isEmail = isLocalSmtp()"/"isEmail = False"/g strelka/runWorkflow.py

python strelka/runWorkflow.py -m local -j 4
mv strelka/results/variants/genome.*.vcf.gz     test.strelka.genome.vcf.gz
mv strelka/results/variants/genome.*.vcf.gz.tbi test.strelka.genome.vcf.gz.tbi
mv strelka/results/variants/variants.vcf.gz     test.strelka.variants.vcf.gz
mv strelka/results/variants/variants.vcf.gz.tbi test.strelka.variants.vcf.gz.tbi

cat <<-END_VERSIONS > versions.yml
"NFCORE_SAREK:SAREK:BAM_VARIANT_CALLING_GERMLINE_ALL:BAM_VARIANT_CALLING_SINGLE_STRELKA:STRELKA_SINGLE":
    strelka: $( configureStrelkaGermlineWorkflow.py --version )
END_VERSIONS
