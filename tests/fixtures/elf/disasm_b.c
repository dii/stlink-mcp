/* The other static scale; see disasm_a.c. */
static int scale(int x) { return x ^ 0x55; }
int scale_b(int x) { return scale(x) + 2; }
/* Longer than the 256 instructions one disassembly returns, for the test of the
 * cut: at -O0 each store is four instructions. */
void long_function(volatile int *p) {
    p[0] = 0;
    p[1] = 1;
    p[2] = 2;
    p[3] = 3;
    p[4] = 4;
    p[5] = 5;
    p[6] = 6;
    p[7] = 7;
    p[8] = 8;
    p[9] = 9;
    p[10] = 10;
    p[11] = 11;
    p[12] = 12;
    p[13] = 13;
    p[14] = 14;
    p[15] = 15;
    p[16] = 16;
    p[17] = 17;
    p[18] = 18;
    p[19] = 19;
    p[20] = 20;
    p[21] = 21;
    p[22] = 22;
    p[23] = 23;
    p[24] = 24;
    p[25] = 25;
    p[26] = 26;
    p[27] = 27;
    p[28] = 28;
    p[29] = 29;
    p[30] = 30;
    p[31] = 31;
    p[32] = 32;
    p[33] = 33;
    p[34] = 34;
    p[35] = 35;
    p[36] = 36;
    p[37] = 37;
    p[38] = 38;
    p[39] = 39;
    p[40] = 40;
    p[41] = 41;
    p[42] = 42;
    p[43] = 43;
    p[44] = 44;
    p[45] = 45;
    p[46] = 46;
    p[47] = 47;
    p[48] = 48;
    p[49] = 49;
    p[50] = 50;
    p[51] = 51;
    p[52] = 52;
    p[53] = 53;
    p[54] = 54;
    p[55] = 55;
    p[56] = 56;
    p[57] = 57;
    p[58] = 58;
    p[59] = 59;
    p[60] = 60;
    p[61] = 61;
    p[62] = 62;
    p[63] = 63;
    p[64] = 64;
    p[65] = 65;
    p[66] = 66;
    p[67] = 67;
    p[68] = 68;
    p[69] = 69;
    p[70] = 70;
    p[71] = 71;
    p[72] = 72;
    p[73] = 73;
    p[74] = 74;
    p[75] = 75;
    p[76] = 76;
    p[77] = 77;
    p[78] = 78;
    p[79] = 79;
}

void _start(void) { for (;;) { } }
