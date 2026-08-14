#!/usr/bin/env bash -C -e -u -o pipefail
printf "%s %s\n" test_1.fastq.gz test-test_L2_1.gz test_2.fastq.gz test-test_L2_2.gz | while read old_name new_name; do
    [ -f "${new_name}" ] || ln -s $old_name $new_name
done

fastqc \
    --quiet \
    --threads 4 \
    --memory 1024 \
    test-test_L2_1.gz test-test_L2_2.gz

cat <<-END_VERSIONS > versions.yml
"NFCORE_SAREK:SAREK:FASTQC":
    fastqc: $( fastqc --version | sed '/FastQC v/!d; s/.*v//' )
END_VERSIONS
