# Installing SST

The links to download SST source code are available at
<http://sst-simulator.org/SSTPages/SSTMainDownloads/>.
This guide is using the most recent SST version (15.0.0) as of September 2025.
The following guide assumes `$SST_CORE_HOME` as the location where SST will be
installed.

## SST-Core

```sh
# Navigate to the sst folder
cd ext/sst
export SST_CORE_HOME=`pwd`
```

### Downloading the SST-Core Source Code

```sh
wget https://github.com/sstsimulator/sst-core/releases/download/v15.0.0_Final/sstcore-15.0.0.tar.gz
tar xzf sstcore-15.0.0.tar.gz
```

### Installing SST-Core

```sh
cd sst-core
# Make sure your system supports MPI
./configure --prefix=$SST_CORE_HOME --with-python=/usr/bin/python3-config
make all -j$(nproc)
make install
```

Update `PATH`,

```sh
export PATH=$SST_CORE_HOME/bin:$PATH
```

## SST-Elements

### Downloading the SST-Elements Source Code

```sh
git clone https://github.com/kaustav-goswami/sst-elements.git
# make sure to use the correct branch
cd sst-elements
checkout cxl-bi
./autogen.sh
cd ..
```

### Installing SST-Elements

```sh
cd sst-elements
./configure --prefix=$SST_CORE_HOME --with-python=/usr/bin/python3-config \
            --with-sst-core=$SST_CORE_HOME
make all -j$(nproc)
make install
```

Adding `PKG_CONFIG_PATH` to `.bashrc` (so pkg-config can find SST .pc file),

```sh
echo "export PKG_CONFIG_PATH=$PKG_CONFIG_PATH:$SST_CORE_HOME/lib/pkgconfig/" >> ~/.bashrc
```

### Building gem5 library

At the root of the gem5 folder, you need to compile gem5 as a library. This
varies  dependent on which OS you are using. If you're using Linux, then
execute the following:
```sh
scons build/ALL/libgem5_opt.so -j $(nproc) --without-tcmalloc --duplicate-sources
```
In case you're using Mac, then type the following:
```sh
scons build/ALL/libgem5_opt.dylib -j $(nproc) --without-tcmalloc --duplicate-sources
```

**Note:**
* `--without-tcmalloc` is required to avoid a conflict with SST's malloc.
* `--duplicate-sources` is required as the compilation of SST depends on sources to be present in the "build" directory.
* The Mac version was tested on a Macbook Air with M2 processor.

### Compiling the SST integration

Go to the SST directory in the gem5 repo.
```sh
cd ext/sst
```
Depending on your OS, you need to copy the correct `Makefile.xxx` file to
`Makefile`.
```sh
cp Makefile.xxx Makefile    # linux or mac
# CXL-ClusterSim works with gem5_ALL
make ARCH=ALL -j4
```
The make file is hardcoded to RISC-V. IN the case you wish to compile to ARM,
edit the Makefile or pass `ARCH=RISCV` to `ARCH=ARM` while compiling.
### Running an example simulation

See `README.md`
