FROM mambaorg/micromamba:1.5.10

USER root
WORKDIR /workspace

RUN micromamba install -y -n base -c conda-forge -c bioconda \
    python=3.12 \
    pip \
    curl \
    wget \
    samtools \
    bcftools \
    bedtools \
    blast \
    bwa \
    minimap2 \
    seqkit \
    diamond \
    entrez-direct \
    biopython \
    pandas \
    numpy \
    scipy \
    scikit-learn \
    r-base \
    && micromamba clean --all --yes

ENV PATH=/opt/conda/bin:$PATH

# bioconda's update_blastdb.pl only looks for curl at /usr/bin or /usr/local/bin,
# but conda installs it at /opt/conda/bin. Symlink it so DB downloads work.
RUN ln -sf /opt/conda/bin/curl /usr/bin/curl

CMD ["/bin/bash"]
